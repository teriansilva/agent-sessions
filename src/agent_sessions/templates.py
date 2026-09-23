"""Instruction templates (#905, P1) — the operator's library of reusable messages.

A template is a *message*, not a prompt: named instruction text, the reference images that
belong with it, and the ``{{field}}`` slots filled at send time. It is sent into a live session
through the composer as user-turn stdin — it never enters ``prompts.py``'s registry and never
becomes a system message. The gallery (P2) and the composer picker (P3) read this store; nothing
in the app acts on a template's content itself.

**Its own file, off the boot path.** ``~/.config/agent-sessions/templates.json`` beside
``prefs.json`` (override: ``AGENT_SESSIONS_TEMPLATES``), mode 0600, every write through
``atomicjson`` under the sidecar lock. Not a ``prefs.json`` block and not on ``/api/config``, for
the reason ``routes/prompts.py`` gives: a library of bodies is tens of KB that only the gallery
reads, and ``/api/config`` is the SPA's boot path.

**Lenient read, strict write, and a corrupt file is never overwritten in place.** A file that
cannot be parsed — or that carries a record the validator refuses — reads as the records that can
be trusted plus a ``damaged`` flag. The first accepted write after such a read **hard-links** the
damaged file to ``templates.json.corrupt-<timestamp>`` and only then publishes the replacement
by atomic rename of the original name. Ordering is the point (Hermes on #906): a rename-aside
*before* the publish left the library with no canonical file at all when the publish failed
(``ENOSPC`` mid-recovery) — the only copy was the quarantine. With the link first, a failed
publish leaves the original exactly where it was, a successful one leaves the bytes under both
names, and no lock-free reader ever sees an empty window. A refused mutation touches nothing.

**A newer store is refused, never rewritten.** ``version`` is checked on every read: a file
written by a later build reads as an empty library (logged) and every write raises
``TemplateStoreUnsupported`` — an older binary must not coerce records it does not understand,
drop the fields it cannot see, and publish the result as version 1.

**Version 2 (#1090, Phase 1) adds ``source`` to a field.** ``template`` (the default, and what
every v1 record reads as) is the field this template declares; ``library`` means its value comes
from the variable of the same name in ``template_vars`` — so it carries no ``default`` of its own,
and one edit to the variable reaches every template that uses it. A v1 file reads without
rewriting; the first accepted write publishes it as v2.

**Version 3 (#1090, Phase 2) adds ``kind`` to a field** — ``text`` (the default) or ``secret``. A
secret field never carries a ``default``, so no secret can be written into this file: with
``source: "library"`` its value is the stored (encrypted) secret variable of that name; with
``source: "template"`` it is typed at send time and never stored. A template with any secret field
is rendered and delivered SERVER-SIDE (``template_send``) — the browser never holds the value. A
v2 build facing this v3 file serves it read-empty and refuses every write, so it can never drop
``kind`` and re-save a secret field as text.

**Optimistic concurrency, not last-write-wins.** ``update_template`` and ``delete_template`` take
the ``updated_at`` the editor last read; a mismatch raises ``TemplateConflict`` carrying the
current record and writes nothing. ``mark_used`` deliberately does not touch ``updated_at`` — a
send is not an edit, and it must not invalidate an editor someone has open.

**Control characters are rejected, not stripped.** ESC is the one that matters: inside a
bracketed paste it can end the paste early and turn the rest of the text into key input (the
#618 class). The author gets a 422 naming the field instead of a silently altered template —
``handoff.sanitize_seed``'s argument for rejecting over truncating, applied one store over.
"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import re
import shutil
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .atomicjson import atomic_write_json, json_write_lock
from .routes.upload import STORED_RE, open_upload, uploads_dir

log = logging.getLogger(__name__)

STORE_VERSION = 3

TEMPLATES_MAX = 200
NAME_MAX = 120
DESCRIPTION_MAX = 300
TAGS_MAX = 8
TAG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
#: One cap for "what you can draft": the same value as ``routes.sessions._DRAFT_TEXT_MAX``, so
#: anything the composer will hold as a draft can be kept as a template.
BODY_MAX = 100_000
FIELDS_MAX = 12
FIELD_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
LABEL_MAX = 60
DEFAULT_MAX = 500
IMAGES_MAX = 8
IMAGE_NAME_MAX = 200
#: The picture formats the read-back route serves (``routes/upload.py``). A template may only
#: reference what the gallery can show — a non-image upload is refused on write, not 404'd later.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
#: Largest number a stored timestamp / counter may carry: exactly representable in a JSON
#: number on both sides of the wire (2**53), and far beyond any real epoch. Anything else —
#: NaN, ±inf, a 400-digit int — is refused on write and skipped on read rather than reaching a
#: ``JSONResponse`` that cannot encode it (Hermes on #906).
NUMBER_MAX = float(2**53)
NUMBER_MAX_INT = 2**53

EDITABLE_KEYS = frozenset({"name", "description", "tags", "body", "fields", "images"})
_FIELD_KEYS = frozenset({"name", "label", "default", "required", "source", "kind"})
#: What a field's value is (store v3, #1090 Phase 2): plain text, or a secret that never lands in
#: this file and never reaches the browser.
FIELD_KINDS = ("text", "secret")
#: Where a field's value comes from (store v2, #1090): this template, or the variables library.
FIELD_SOURCES = ("template", "library")
_IMAGE_KEYS = frozenset({"name", "path"})

# C0 controls other than TAB / LF, plus DEL and the C1 range. CR never reaches this check: CRLF
# and lone CR are normalized to LF first, which only ever shrinks the text.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

#: What the client may show as caps. Served with the library so the editor needs no copy.
LIMITS = {
    "templates_max": TEMPLATES_MAX,
    "name_max": NAME_MAX,
    "description_max": DESCRIPTION_MAX,
    "tags_max": TAGS_MAX,
    "body_max": BODY_MAX,
    "fields_max": FIELDS_MAX,
    "label_max": LABEL_MAX,
    "default_max": DEFAULT_MAX,
    "images_max": IMAGES_MAX,
    "image_suffixes": sorted(IMAGE_SUFFIXES),
}


class TemplateError(ValueError):
    """A write refused by a rule in this module — the route answers 422 with the message."""


class TemplateNotFound(LookupError):
    """No template with that id."""


class TemplateConflict(Exception):
    """The record changed since the editor read it. ``current`` is what is stored now."""

    def __init__(self, current: dict) -> None:
        super().__init__("template changed since it was read")
        self.current = current


class TemplateStoreUnsupported(RuntimeError):
    """The file on disk is a newer store version than this build writes. Reads serve an empty
    library; every write refuses, so nothing this build cannot represent is ever rewritten."""

    def __init__(self, version: object) -> None:
        super().__init__(
            f"templates.json is store version {version!r}, newer than this build "
            f"(version {STORE_VERSION}) — upgrade BattleLab; nothing was written"
        )
        self.version = version


def store_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_TEMPLATES",
            str(Path.home() / ".config" / "agent-sessions" / "templates.json"),
        )
    )


# ---- validation -----------------------------------------------------------------------------


def _text(
    payload: dict, key: str, *, max_len: int, required: bool = False, multiline: bool = False
) -> str:
    raw = payload.get(key, "")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise TemplateError(f"{key} must be a string")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if _CONTROL_RE.search(raw):
        raise TemplateError(f"{key} contains a control character")
    if _SURROGATE_RE.search(raw):
        # A lone UTF-16 surrogate survives `json.loads` but not `.encode("utf-8")` — it would
        # surface as a 500 at write time (or, on a send, after the prompt line was cleared).
        raise TemplateError(f"{key} contains an invalid character")
    if not multiline and "\n" in raw:
        raise TemplateError(f"{key} must be a single line")
    if len(raw) > max_len:
        raise TemplateError(f"{key} is too long (max {max_len} characters)")
    value = raw if multiline else raw.strip()
    if required and not value.strip():
        raise TemplateError(f"{key} is required")
    return value


def _tags(raw: object) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TemplateError("tags must be a list")
    if len(raw) > TAGS_MAX:
        raise TemplateError(f"too many tags (max {TAGS_MAX})")
    out: list[str] = []
    for tag in raw:
        if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
            raise TemplateError(
                "a tag is lowercase letters, digits, '-' or '_' (max 24 characters)"
            )
        if tag in out:
            raise TemplateError(f"duplicate tag: {tag}")
        out.append(tag)
    return out


def _fields(raw: object) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TemplateError("fields must be a list")
    if len(raw) > FIELDS_MAX:
        raise TemplateError(f"too many fields (max {FIELDS_MAX})")
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise TemplateError("a field must be an object")
        unknown = sorted(set(item) - _FIELD_KEYS)
        if unknown:
            raise TemplateError(f"unknown field keys: {unknown}")
        name = item.get("name")
        if not isinstance(name, str) or not FIELD_NAME_RE.fullmatch(name):
            raise TemplateError(
                "a field name is a lowercase letter followed by letters, digits or '_' "
                "(max 32 characters)"
            )
        if name in seen:
            raise TemplateError(f"duplicate field: {name}")
        seen.add(name)
        label = _text(item, "label", max_len=LABEL_MAX) or name
        default = _text(item, "default", max_len=DEFAULT_MAX, multiline=True)
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise TemplateError(f"field {name}: required must be true or false")
        source = item.get("source", "template")
        if source not in FIELD_SOURCES:
            raise TemplateError(f"field {name}: source must be 'template' or 'library'")
        if source == "library" and default:
            # One owner for the value: a library field whose own default differed from the
            # variable would make "what gets sent" depend on which one the reader looked at.
            raise TemplateError(f"field {name}: a library field takes its value from the library")
        kind = item.get("kind", "text")
        if kind not in FIELD_KINDS:
            raise TemplateError(f"field {name}: kind must be 'text' or 'secret'")
        if kind == "secret" and default:
            # The whole point: a secret is never written into templates.json.
            raise TemplateError(f"field {name}: a secret field has no default")
        out.append(
            {
                "name": name,
                "label": label,
                "default": default,
                "required": required,
                "source": source,
                "kind": kind,
            }
        )
    return out


def _images(raw: object, *, check_files: bool) -> list[dict]:
    """The image references, validated against the read-back contract.

    Containment is textual, never ``resolve()``d: the read-back binds the folder by descriptor
    and never resolves a path, so neither does this (a symlinked uploads folder or a symlink
    entry is refused by ``open_upload``, not followed and then "contained"). ``check_files``
    is the write-time half — the entry must open as a regular file through the bound folder —
    and is OFF on read: a stored record whose image was deleted later is still a template with
    a body, and must not be reclassified as corrupt and rewritten away (Hermes on #906). Its
    thumbnail simply 404s.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TemplateError("images must be a list")
    if len(raw) > IMAGES_MAX:
        raise TemplateError(f"too many images (max {IMAGES_MAX})")
    updir = uploads_dir()
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TemplateError("an image must be an object")
        unknown = sorted(set(item) - _IMAGE_KEYS)
        if unknown:
            raise TemplateError(f"unknown image keys: {unknown}")
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise TemplateError("an image path must be a non-empty string")
        p = Path(path)
        if not p.is_absolute() or ".." in p.parts or updir not in p.parents:
            raise TemplateError("image outside the upload folder")
        if p.parent != updir:
            raise TemplateError("an image must sit directly inside the upload folder")
        if not STORED_RE.fullmatch(p.name):
            raise TemplateError("an image must be a file the upload route wrote")
        if p.suffix.lower() not in IMAGE_SUFFIXES:
            raise TemplateError("an image must be a png, jpg, gif or webp upload")
        if check_files:
            try:
                fd, _ = open_upload(p.name)
            except OSError as e:
                # ELOOP: a symlink at the entry. ENOTDIR: a symlink where the FOLDER should be
                # (Linux answers `O_DIRECTORY | O_NOFOLLOW` on a symlink-to-directory with
                # ENOTDIR, measured in filewrite.py). Both mean "not inside the real folder".
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise TemplateError("image outside the upload folder") from None
                raise TemplateError("image does not exist") from None
            os.close(fd)
        name = _text(item, "name", max_len=IMAGE_NAME_MAX) or p.name
        out.append({"name": name, "path": str(p)})
    return out


def validate(payload: object, *, check_files: bool = True) -> dict:
    """The editable fields of a template, validated and normalized, or ``TemplateError``.

    Unknown keys are refused rather than ignored (the rule every prefs validator follows), so
    a server-owned field sent as input — ``id``, ``updated_at``, ``used_count`` — is a 422, not a
    silent no-op. ``check_files`` is the write-time image existence check (see ``_images``).
    """
    if not isinstance(payload, dict):
        raise TemplateError("expected a JSON object")
    unknown = sorted(set(payload) - EDITABLE_KEYS)
    if unknown:
        raise TemplateError(f"unknown fields: {unknown}")
    return {
        "name": _text(payload, "name", max_len=NAME_MAX, required=True),
        "description": _text(payload, "description", max_len=DESCRIPTION_MAX),
        "tags": _tags(payload.get("tags")),
        "body": _text(payload, "body", max_len=BODY_MAX, required=True, multiline=True),
        "fields": _fields(payload.get("fields")),
        "images": _images(payload.get("images"), check_files=check_files),
    }


def _number(value: object, what: str) -> float:
    """A finite number in ``[0, 2**53]``, as a float — or ``TemplateError``.

    An ``int`` is bounded BEFORE it is converted: ``float(2**53 + 1)`` is ``2**53.0``, so a
    bound applied after conversion accepted one-past-the-maximum and let a fence "match" a
    record it did not equal (Hermes on #906, round 3).
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TemplateError(f"{what} must be a number")
    if isinstance(value, int):
        if value < 0 or value > NUMBER_MAX_INT:
            raise TemplateError(f"{what} is out of range")
        return float(value)
    if not math.isfinite(value) or value < 0 or value > NUMBER_MAX:
        raise TemplateError(f"{what} is out of range")
    return value


def _coerce_record(raw: object) -> dict:
    """A stored record, re-validated on read. Anything this refuses is skipped by ``_read``."""
    if not isinstance(raw, dict):
        raise TemplateError("record is not an object")
    editable = validate({k: raw.get(k) for k in EDITABLE_KEYS if k in raw}, check_files=False)
    tid = raw.get("id")
    if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
        raise TemplateError("record has no valid id")
    used = raw.get("used_count", 0)
    if isinstance(used, bool) or not isinstance(used, int) or used < 0 or used > NUMBER_MAX:
        raise TemplateError("used_count must be a non-negative integer")
    last_used = raw.get("last_used_at")
    return {
        **editable,
        "id": tid,
        "created_at": _number(raw.get("created_at"), "created_at"),
        "updated_at": _number(raw.get("updated_at"), "updated_at"),
        "used_count": used,
        "last_used_at": None if last_used is None else _number(last_used, "last_used_at"),
    }


# ---- store I/O -------------------------------------------------------------------------------


def _read(path: Path) -> tuple[list[dict], bool]:
    """The trustworthy records plus whether the file needs quarantining before the next write."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], False
    except (OSError, ValueError) as e:
        log.warning("templates: %s is unreadable (%s); serving an empty library", path, e)
        return [], True
    if not isinstance(raw, dict):
        log.warning("templates: %s is not a template store; serving an empty library", path)
        return [], True
    # Version BEFORE shape: a future store need not carry a v1 ``templates`` list at all, and a
    # shape check first would call it damaged and let the next write quarantine + downgrade it
    # (Hermes on #906). Only a mapping that claims a version this build understands is read.
    version = raw.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > STORE_VERSION:
        raise TemplateStoreUnsupported(version)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        log.warning("templates: %s carries no valid store version; serving an empty library", path)
        return [], True
    if not isinstance(raw.get("templates"), list):
        log.warning("templates: %s is not a template store; serving an empty library", path)
        return [], True
    records: list[dict] = []
    seen: set[str] = set()
    damaged = False
    for item in raw["templates"]:
        try:
            rec = _coerce_record(item)
        except TemplateError as e:
            log.warning("templates: skipping an unreadable record in %s: %s", path, e)
            damaged = True
            continue
        if rec["id"] in seen:
            log.warning("templates: skipping a duplicate id %r in %s", rec["id"], path)
            damaged = True
            continue
        seen.add(rec["id"])
        records.append(rec)
    return records, damaged


def _quarantine(path: Path) -> Path:
    """Keep the damaged bytes under a second name WITHOUT moving them away from the first.

    A hard link is the whole trick: it is atomic, costs no copy, and leaves ``path`` in place,
    so the publish that follows is an ordinary atomic replace of a name that still exists —
    and if that publish fails, nothing has moved. A filesystem that refuses links falls back
    to a copy, which preserves the same property at the cost of the bytes.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(1000):
        suffix = f".corrupt-{stamp}" + (f"-{n}" if n else "")
        target = path.with_name(path.name + suffix)
        try:
            os.link(path, target)
        except FileExistsError:
            continue
        except OSError:
            shutil.copy2(path, target)
        log.warning("templates: kept the unreadable store %s aside as %s", path, target)
        return target
    raise OSError(f"could not find a free quarantine name for {path}")


def _write(path: Path, records: list[dict]) -> None:
    atomic_write_json(path, {"version": STORE_VERSION, "templates": records}, mode=0o600)


def _mutate(fn: Callable[[list[dict]], dict | None]) -> dict | None:
    """Read-modify-write the whole library under the sidecar lock.

    ``fn`` edits ``records`` in place and returns the record the caller wants back. It runs
    BEFORE the quarantine, so a refused mutation (validation, a stale edit) leaves a damaged file
    untouched; only an accepted one links it aside and then publishes the new document over
    the original name. A newer store version raises out of ``_read`` before anything happens.
    """
    path = store_path()
    # Under the write seam's cross-process fence: a template send's fingerprint re-reads these
    # revisions inside it right before byte one (#1090, Hermes on #1105).
    from . import session_input

    with session_input.mutation_fence(), json_write_lock(path):
        records, damaged = _read(path)
        result = fn(records)
        if damaged and path.exists():
            _quarantine(path)
        _write(path, records)
    return result


def _sorted(records: list[dict]) -> list[dict]:
    return sorted(
        records,
        key=lambda r: (-(r["last_used_at"] or 0.0), -r["updated_at"], r["name"].lower()),
    )


def _find(records: list[dict], tid: str) -> dict:
    for rec in records:
        if rec["id"] == tid:
            return rec
    raise TemplateNotFound(tid)


def _mint_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].strip("-") or "template"
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _bumped(previous: float) -> float:
    """A new ``updated_at`` strictly later than the previous one, whatever the clock says —
    or a refusal. ``previous + 1e-3`` rounded back to ``previous`` at the top of the accepted
    range, so two edits carrying the same stale fence both went through (Hermes on #906). The
    next representable float is strictly greater by construction; if it would leave the range
    every read accepts, the record's revision counter is exhausted and the edit is refused
    before anything is written."""
    now = time.time()
    candidate = now if now > previous else math.nextafter(previous, math.inf)
    if candidate > NUMBER_MAX:
        raise TemplateError("this template's revision counter is exhausted; it cannot be edited")
    return candidate


def parse_fence(value: object) -> float:
    """``expected_updated_at`` as the wire carries it (a JSON number, or the DELETE query
    string), under the same finite / non-negative / ≤ 2**53 rule every stored timestamp
    passes — so a 400-digit int or ``1e400`` is a 422, never a ``float()`` overflow rendered
    as a 500. The query string is read as an exact decimal so ``9007199254740993`` cannot
    round down onto ``2**53`` before it is bounded (Hermes on #906, round 3)."""
    if isinstance(value, str):
        try:
            exact = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            raise TemplateError("expected_updated_at must be a number") from None
        if not exact.is_finite() or exact < 0 or exact > NUMBER_MAX_INT:
            raise TemplateError("expected_updated_at is out of range")
        return float(exact)
    return _number(value, "expected_updated_at")


# ---- public API ------------------------------------------------------------------------------


def _read_leniently(path: Path) -> list[dict]:
    """The read-side contract: a store this build cannot understand is an empty library."""
    try:
        records, _ = _read(path)
    except TemplateStoreUnsupported as e:
        log.warning("templates: %s; serving an empty library", e)
        return []
    return records


def list_templates() -> list[dict]:
    """Every readable template, most recently used first (then most recently edited)."""
    return _sorted(_read_leniently(store_path()))


class TemplateInventoryIncomplete(RuntimeError):
    """The template library could not be read in full, so "who uses this variable?" has no
    trustworthy answer. A destructive caller must refuse rather than read it as "nobody"."""


def _references(records: list[dict], name: str) -> list[dict]:
    return [
        {"id": t["id"], "name": t["name"]}
        for t in _sorted(records)
        if any(f["source"] == "library" and f["name"] == name for f in t["fields"])
    ]


def library_references(name: str) -> list[dict]:
    """Every template with a ``library`` field named ``name`` — ``[{id, name}]``, gallery order.

    DISPLAY ONLY (a variable's ``used by`` count): lenient like every read, so an unreadable,
    damaged or newer store simply names fewer dependants. Anything that DESTROYS on the answer
    uses ``library_references_checked`` instead (Hermes on #1095).
    """
    return _references(_read_leniently(store_path()), name)


def has_secret(template: dict) -> bool:
    """Whether a template must be sent server-side (any secret field)."""
    return any(f["kind"] == "secret" for f in template["fields"])


def library_usage() -> dict[str, list[dict]]:
    """``{variable name: [{id, name}]}`` for every library field in the library, from ONE
    lenient read — what the variables list shows as ``used by``. Reading the store once per
    variable made the list O(variables × library): 24 s at the caps, measured (#1095 review).
    """
    usage: dict[str, list[dict]] = {}
    for t in _sorted(_read_leniently(store_path())):
        for name in {f["name"] for f in t["fields"] if f["source"] == "library"}:
            usage.setdefault(name, []).append({"id": t["id"], "name": t["name"]})
    return usage


def library_references_checked(name: str) -> list[dict]:
    """``library_references``, but fail-closed: an unreadable file, a damaged record or a newer
    store version raises ``TemplateInventoryIncomplete`` rather than answering "no dependants".

    A missing file is a real answer (no templates, so none use it). Everything else that
    ``_read`` degrades to "the records it could trust" is an INCOMPLETE inventory — a dependant
    may sit in exactly the part that could not be read — and deleting on it would strip a live
    template of its value (Hermes on #1095 reproduced it with EACCES and a v3 store).
    """
    try:
        records, damaged = _read(store_path())
    except TemplateStoreUnsupported as e:
        raise TemplateInventoryIncomplete(str(e)) from None
    if damaged:
        raise TemplateInventoryIncomplete("the template library could not be read in full")
    return _references(records, name)


def get_template(tid: str) -> dict:
    return _find(_read_leniently(store_path()), tid)


def create_template(payload: object) -> dict:
    fields = validate(payload)

    def fn(records: list[dict]) -> dict:
        if len(records) >= TEMPLATES_MAX:
            raise TemplateError(f"too many templates (max {TEMPLATES_MAX})")
        now = time.time()
        rec = {
            **fields,
            "id": _mint_id(fields["name"], {r["id"] for r in records}),
            "created_at": now,
            "updated_at": now,
            "used_count": 0,
            "last_used_at": None,
        }
        records.append(rec)
        return dict(rec)

    return _mutate(fn)  # type: ignore[return-value]


def update_template(tid: str, payload: object, expected_updated_at: float) -> dict:
    """Replace the editable fields, or raise ``TemplateConflict`` if the record moved."""
    fields = validate(payload)

    def fn(records: list[dict]) -> dict:
        rec = _find(records, tid)
        if rec["updated_at"] != expected_updated_at:
            raise TemplateConflict(dict(rec))
        rec.update(fields)
        rec["updated_at"] = _bumped(rec["updated_at"])
        return dict(rec)

    return _mutate(fn)  # type: ignore[return-value]


def delete_template(tid: str, expected_updated_at: float) -> None:
    def fn(records: list[dict]) -> None:
        rec = _find(records, tid)
        if rec["updated_at"] != expected_updated_at:
            raise TemplateConflict(dict(rec))
        records.remove(rec)
        return None

    _mutate(fn)


def mark_used(tid: str) -> dict:
    """Bump the usage counters. ``updated_at`` is left alone: a send is not an edit."""

    def fn(records: list[dict]) -> dict:
        rec = _find(records, tid)
        # Saturate at the accepted maximum rather than write a count the next read refuses
        # (Hermes on #906: 2**53 + 1 made the whole template unreadable).
        rec["used_count"] = min(rec["used_count"] + 1, int(NUMBER_MAX))
        rec["last_used_at"] = time.time()
        return dict(rec)

    return _mutate(fn)  # type: ignore[return-value]


__all__ = [
    "BODY_MAX",
    "DEFAULT_MAX",
    "DESCRIPTION_MAX",
    "EDITABLE_KEYS",
    "FIELDS_MAX",
    "IMAGES_MAX",
    "IMAGE_SUFFIXES",
    "LABEL_MAX",
    "LIMITS",
    "NAME_MAX",
    "TAGS_MAX",
    "TEMPLATES_MAX",
    "NUMBER_MAX",
    "NUMBER_MAX_INT",
    "TemplateConflict",
    "TemplateError",
    "TemplateInventoryIncomplete",
    "TemplateNotFound",
    "TemplateStoreUnsupported",
    "create_template",
    "FIELD_KINDS",
    "FIELD_SOURCES",
    "delete_template",
    "get_template",
    "has_secret",
    "library_references",
    "library_references_checked",
    "library_usage",
    "list_templates",
    "mark_used",
    "parse_fence",
    "store_path",
    "update_template",
    "validate",
]
