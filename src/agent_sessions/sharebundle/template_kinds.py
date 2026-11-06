"""Templates and variable definitions in a share bundle (#1714 P3).

A **variable** travels as its global definition (`variable.json`): its kind, its typed
definition (type, label, description, a non-secret default) and, for a text variable only, its
value. A secret or a secret file travels as a definition with **no value in any form**: it arrives
empty, for the operator to fill in. Project bindings, collections and agent access never travel.

A **template** travels as its message (`template.json`) plus its reference images
(`images/<n>.<ext>`). Every `source: library` field is a dependency on the variable of that name,
and every one must travel in the same file: a template is never bound by name to a variable that
happens to exist on the importing install.

**Nothing secret leaves.** Every text value of either kind — defaults included — is scanned on
export, against this install's stored secrets and against credential-value shapes; a hit refuses
that item's export and names the field, never the value. The scan fails closed: when the secret
inventory can't be read, nothing of either kind is exported.

Nothing here activates anything: an imported template is never sent, and an imported variable is
bound to no project and offered to no agent.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from .. import session_input, template_secrets, template_vars, templates, variable_types, variables
from ..gallery import contract as gallery_contract
from ..playbooks import store
from ..template_send import FIELD_TOKEN_RE

log = logging.getLogger(__name__)

TEMPLATE_FILE = "template.json"
VARIABLE_FILE = "variable.json"
FILE_FORMAT = 1
IMAGES_DIR = "images"
#: Each reference image is one bundle file, so the format's per-file bound is its bound too.
IMAGE_MAX_BYTES = 4 * 1024 * 1024
_TEMPLATE_KEYS = frozenset({"format", "name", "description", "tags", "body", "fields", "images"})
_VARIABLE_KEYS = frozenset({"format", "kind", "definition", "value"})
_IMAGE_KEYS = frozenset({"name", "file"})
#: The leading bytes each image suffix the template store accepts must start with: an import
#: never stores a file the gallery would serve under a type its bytes are not.
_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
}


class Refused(ValueError):
    """This item can't be exported or imported; the message is the reason, in words.

    `withhold_id`: the item's own ID or name is what holds the secret, so no answer may echo it
    (the export refusal names the item without it)."""

    withhold_id = False
    #: The scan refused it: none of the item's own text may be shown (its title included).
    scanned = False


class Terminal(store.Conflict):
    """A publication that the same plan can never complete (the store refuses the planned name
    now): the remedy is a new preview, never "confirm again"."""


# ---- the export-side credential scan ----------------------------------------------------------


class Inventory(list):
    """The redaction inventory (`redaction_values()`, fragments included) plus `whole`: every
    secret as stored or typed, before it was split into fragments. Folding judges `whole`, so a
    value's provenance decides, never whether some file happens to hold a line equal to it."""

    whole: tuple[str, ...] = ()


def secret_inventory() -> Inventory:
    """Every stored secret to scan against, or `Refused` when it can't be read (fail closed)."""
    from ..connectors import store as connector_store

    try:
        inventory = Inventory(template_secrets.redaction_values())
        # Read AFTER the redaction set: a secret stored in between only adds to what is folded.
        now = time.time()
        # Typed-once values as REGISTERED (`template_secrets.typed_whole`): provenance, never
        # recovered from the redaction set, where a value and an equal fragment are one entry.
        typed = template_secrets.typed_whole(now)
        inventory.whole = (
            *template_vars.secret_values_checked(),
            *connector_store.token_values_checked(),
            *typed,
        )
    except Exception as exc:  # noqa: BLE001 — any failure to establish the set refuses
        log.warning("share export: the secret check can't run (%s)", type(exc).__name__)
        raise Refused(
            "the secret check can't run (your secrets store can't be read), so nothing that "
            "might hold a secret is exported"
        ) from None
    return inventory


_FIXED_BY = {
    "ID": "duplicate the template under another name and export the copy",
    "variable name": "create it again under another name to export it",
    "field names": "rename that field (or, for a library field, the variable it uses), then "
    "export it",
}
#: Texts that name the item itself: a refusal over them never echoes the item's ID or name.
_WITHHOLD = frozenset({"ID", "variable name"})


#: Identifier texts: a slug (a template ID is the lowercased name with every other run of
#: characters turned into `-`) or a lowercase name, and tags (lowercase, typed). A secret is
#: matched there in that same folded form too.
_IDENTIFIERS = frozenset({"ID", "variable name", "field names", "tag"})
_FOLD_RE = re.compile(r"[^a-z0-9]+")
#: A minted ID is cut at 48 (`templates._mint_id`, after which `-` is stripped) and a renamed
#: import at 64 less its `-<n>`: a base this long may be a cut slug.
_CUT_AT = 47
_SUFFIX_RE = re.compile(r"-[0-9]+$")


def _fold(text: str) -> str:
    return _FOLD_RE.sub("-", text.lower()).strip("-")


#: One inventory's folded forms, kept only while the caller passes that same list object (a
#: catalog judges every row against one list): never a second inventory beyond the call's own.
#: Rebound whole, never mutated, so concurrent calls never mix two inventories.
_FOLDED: tuple = (None, (), {})


def _foldable(secrets: list[str]) -> set[str]:
    """The values folding judges: every whole secret, and the credential runs (20+ characters)
    of a multi-line one, and each of its lines that is itself a credential (`_credential_line`).
    Structure lines are not folded: `metadata:` stays redacted verbatim (the fragments
    contract), but folded it would be the word `metadata`."""
    whole = getattr(secrets, "whole", None)
    if whole is None:
        return set(secrets)  # a bare list (no provenance): fold all of it, the safe side
    out: set[str] = set()
    for v in whole:
        v = v.strip()  # `echo token > file` is one token and a newline: a single-line secret
        if "\n" in v:
            out.update(template_secrets._FRAGMENT_RE.findall(v))
            out.update(line.strip() for line in v.splitlines() if _credential_line(line.strip()))
        elif v:
            out.add(v)
    return out


def _credential_line(line: str) -> bool:
    """A line of a multi-line secret that is itself a credential, not structure: one word (no
    whitespace), no `key: value` / `key=value` punctuation, letters AND digits, `SECRET_MIN`
    long. `metadata:`, `kind: Secret` or `deployer` are structure; `Ab12cdEF34gh` is a token."""
    return (
        len(line) >= template_secrets.SECRET_MIN
        and not any(c.isspace() for c in line)
        and ":" not in line
        and "=" not in line
        and any(c.isalpha() for c in line)
        and any(c.isdigit() for c in line)
    )


def _folded(secrets: list[str]) -> tuple[tuple[str, ...], dict[str, list[str]]]:
    """Each foldable secret's folded form, at least `SECRET_MIN` long, and the forms by their
    first `SECRET_MIN` characters (both linear in the inventory)."""
    global _FOLDED
    entry = _FOLDED
    if entry[0] is not secrets:
        n = template_secrets.SECRET_MIN
        forms = tuple(f for f in {_fold(v) for v in _foldable(secrets)} if len(f) >= n)
        heads: dict[str, list[str]] = {}
        for f in forms:
            heads.setdefault(f[:n], []).append(f)
        entry = (secrets, forms, heads)
        _FOLDED = entry
    return entry[1], entry[2]


def _holds_folded(what: str, text: str, secrets: list[str]) -> bool:
    forms, heads = _folded(secrets)
    t = _fold(text)
    if any(f in t for f in forms):
        return True
    if what != "ID":
        return False  # only a template ID is ever cut: a typed name can't hold just a START
    n = template_secrets.SECRET_MIN
    # A cut slug may end with the start of a secret. Its base before a rename's `-<n>` is
    # checked, and the whole ID too (a cut can end in a secret's own digits).
    for cand in {t, _SUFFIX_RE.sub("", t)}:
        if len(cand) < _CUT_AT:
            continue
        if any(
            f.startswith(cand[i:])
            for i in range(len(cand) - n + 1)
            for f in heads.get(cand[i : i + n], ())
        ):
            return True
    return False


def scan(texts: list[tuple[str, str]], secrets: list[str]) -> None:
    """Refuse when any `(what, text)` holds a stored secret or a credential-shaped value."""
    for what, text in texts:
        if not text:
            continue
        held = template_secrets.redact_text(text, secrets) != text or (
            what in _IDENTIFIERS and _holds_folded(what, text, secrets)
        )
        if not held and not variables.label_value_shaped(text):
            continue
        subject = "one of its field names" if what == "field names" else f"its {what}"
        problem = (
            f"{subject} holds one of your stored secrets"
            if held
            else f"{subject} looks like a credential (a key, token or password)"
        )
        # A template's ID is minted from its name once, and a variable's name never changes:
        # neither can be edited away, so the fix is a copy under another name.
        fix = _FIXED_BY.get(what, "remove it to export")
        refused = Refused(f"{problem}; {fix}")
        refused.withhold_id = what in _WITHHOLD
        refused.scanned = True
        raise refused


def _definition_texts(definition: dict) -> list[tuple[str, str]]:
    out = [("label", definition.get("label") or ""), ("description", definition.get("help") or "")]
    for key in ("default", "example", "pattern"):
        value = definition.get(key)
        if value is not None and not isinstance(value, bool):
            out.append((key, str(value)))  # a number is text to a reader too
    out.extend(("choice", c) for c in definition.get("choices", []) if isinstance(c, str))
    return out


def _template_texts(doc: dict) -> list[tuple[str, str]]:
    out = [("name", doc["name"]), ("description", doc["description"]), ("body", doc["body"])]
    if doc.get("id"):
        out.insert(0, ("ID", doc["id"]))
    out.extend(("tag", t) for t in doc["tags"])
    # Field names first, and without the name in the label: the refusal must not echo it.
    out.extend(("field names", f["name"]) for f in doc["fields"])
    for f in doc["fields"]:
        out.append((f"field {f['name']} label", f["label"]))
        out.append((f"field {f['name']} default", f["default"]))
    out.extend(("image name", i["name"]) for i in doc["images"])
    return out


# ---- digests ----------------------------------------------------------------------------------


def _digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _template_content(doc: dict, images: list[bytes | None]) -> str:
    """The digest of what a template holds: its editable fields, each image by name and bytes.
    What `prepare` journals before publication and `at` reads back from the store must agree."""
    return _digest(
        {
            **{k: doc[k] for k in ("name", "description", "tags", "body", "fields")},
            "images": [
                {"name": i["name"], "sha256": hashlib.sha256(d).hexdigest() if d else None}
                for i, d in zip(doc["images"], images, strict=True)
            ],
        }
    )


def _variable_content(kind: str, definition: dict, value: object) -> str:
    """A variable's digest: kind, definition and (text only) value. A secret's value or envelope
    never takes part, so filling it in after an import changes nothing here."""
    return _digest({"kind": kind, "definition": definition, "value": value})


# ---- images -----------------------------------------------------------------------------------


def _image_ok(suffix: str, data: bytes) -> bool:
    if suffix == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return any(data.startswith(s) for s in _SIGNATURES.get(suffix, ()))


def _read_upload(stored: str) -> bytes | None:
    """An upload's bytes through the bound folder (no-follow), `None` when it can't be read."""
    from ..routes.upload import open_upload

    try:
        fd, st = open_upload(stored)
    except OSError:
        return None
    try:
        if st.st_size > IMAGE_MAX_BYTES:
            raise Refused(f"an image is larger than {IMAGE_MAX_BYTES // (1024 * 1024)} MiB")
        chunks, left = [], st.st_size + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    return data if len(data) == st.st_size else None


def _probe_upload(stored: str) -> tuple[int, bytes] | None:
    """An upload's size and first bytes (the export picker's cheap check), `None` if unreadable."""
    from ..routes.upload import open_upload

    try:
        fd, st = open_upload(stored)
    except OSError:
        return None
    try:
        return st.st_size, os.read(fd, 16)
    except OSError:
        return None
    finally:
        os.close(fd)


def _remove_uploads(names: list[str]) -> None:
    """Remove the uploads a failed import just wrote (its own new files, nothing else)."""
    from ..routes.upload import open_uploads_dir

    if not names:
        return
    try:
        dir_fd = open_uploads_dir()
    except OSError:
        return
    try:
        for name in names:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


# ---- variables --------------------------------------------------------------------------------


def _variable_records() -> list[dict]:
    """The global variable records, read strictly (a partial library is never exported)."""
    try:
        records = template_vars._read_strictly_full()
    except template_vars.ResolutionUnavailable:
        raise store.StoreError("the variables library can't be read in full", status=503) from None
    return [r for r in records if template_vars._is_global(r)]


def _variable_view(rec: dict) -> tuple[str, dict, object]:
    """`(kind, definition, value)` of a stored global variable: the value only for a set text."""
    kind = rec["kind"]
    definition = variable_types.metadata(rec["name"], kind, rec.get("definition", {}))
    value = None
    if kind == "text" and not rec.get("unset"):
        value = variable_types.stored_value(
            rec["name"], definition, rec["value"], max_len=template_vars.VALUE_MAX
        )
    return kind, definition, value


def variable_ids() -> dict[str, str | None]:
    return {r["name"]: _variable_content(*_variable_view(r)) for r in _variable_records()}


def variable_export(name: str, secrets: list[str]) -> dict:
    rec = next((r for r in _variable_records() if r["name"] == name), None)
    if rec is None:
        raise store.NotFound(name)
    # The name is judged first (a secret stored after the variable was created can match it),
    # before anything whose refusal could quote it.
    scan([("variable name", name)], secrets)
    kind, definition, value = _variable_view(rec)
    texts = _definition_texts(definition)
    if value is not None:
        texts.append(("value", variable_types.text(value)))
    scan(texts, secrets)
    doc = {"format": FILE_FORMAT, "kind": kind, "definition": definition, "value": value}
    return {
        "title": definition.get("label") or name,
        "files": {VARIABLE_FILE: _json_bytes(doc)},
        "depends_on": [],
        "kind": kind,
    }


def _json_bytes(doc: dict) -> bytes:
    return (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _json_doc(files: dict[str, bytes], name: str) -> dict:
    try:
        doc = json.loads(
            files[name].decode("utf-8"),
            object_pairs_hook=gallery_contract._pairs,
            parse_float=gallery_contract._no_float,
            parse_constant=gallery_contract._no_float,
        )
    except (ValueError, UnicodeError, RecursionError, gallery_contract.GalleryError):
        raise Refused(f"{name} is not unambiguous JSON") from None
    if not isinstance(doc, dict):
        raise Refused(f"{name} is not an object")
    fmt_no = doc.get("format")
    if isinstance(fmt_no, int) and not isinstance(fmt_no, bool) and fmt_no > FILE_FORMAT:
        raise Refused("it was made by a newer BattleLab; update to import it")
    if fmt_no != FILE_FORMAT or isinstance(fmt_no, bool):
        raise Refused(f"{name} doesn't have the fields of format 1")
    return doc


def variable_doc(name: str, files: dict[str, bytes]) -> dict:
    """An incoming variable, checked by the library's own rules, or `Refused`. A secret that
    carries a value — in any form — is refused: secret values never travel (P5 is unapproved)."""
    if set(files) != {VARIABLE_FILE}:
        raise Refused(f"a variable is one file, {VARIABLE_FILE}")
    doc = _json_doc(files, VARIABLE_FILE)
    if set(doc) != _VARIABLE_KEYS:
        raise Refused(f"{VARIABLE_FILE} doesn't have the fields of format 1")
    try:
        template_vars._name(name)
        kind = template_vars._kind(doc["kind"])
        if doc["kind"] is None:
            raise template_vars.VariableError("kind must be 'text', 'secret' or 'secret_file'")
        definition = variables._definition(name, kind, doc["definition"])
        value = doc["value"]
        if value is not None:
            if kind != "text":
                raise Refused("a secret travels without its value, and this one carries one")
            value = variable_types.typed_value(
                name, definition, value, max_len=template_vars.VALUE_MAX
            )
            template_vars._value({"value": variable_types.text(value)})
    except (template_vars.VariableError, variable_types.DefinitionError) as exc:
        raise Refused(f"not a valid variable here: {exc}") from None
    return {"kind": kind, "definition": definition, "value": value}


def variable_check_labels(name: str, doc: dict) -> None:
    """The library's own rule for what agents and the access log see: no label that looks like a
    credential or holds a stored secret (`variables._withhold_metadata`)."""
    try:
        variables._withhold_metadata(name, doc["definition"])
    except template_vars.VariableError as exc:
        raise Refused(f"not a valid variable here: {exc}") from None
    except template_secrets.RedactionUnavailable:
        raise Refused(
            "the secret check can't run here (your secrets store can't be read)"
        ) from None


def variable_reserved(name: str) -> bool:
    """Whether the library refuses a new global variable `name` although none holds it: a template
    field or a project binding (current or retained by a deployment) already names it, and a new
    variable must never silently become their value."""
    if any(r["name"] == name for r in _variable_records()):
        return True
    try:
        if templates.library_references_checked(name):
            return True
        records = template_vars._read_strictly_full()
        return bool(template_vars._project_dependants(records, name))
    except (templates.TemplateInventoryIncomplete, template_vars.ResolutionUnavailable):
        return True  # can't tell: never plan onto it


def variable_prepare(name: str, files: dict[str, bytes]) -> tuple[dict, str]:
    doc = variable_doc(name, files)
    return doc, _variable_content(doc["kind"], doc["definition"], doc["value"])


def variable_publish(dest: str, doc: dict) -> str:
    """Create the global variable `dest`: a definition, plus its value for a text variable. A
    secret is created without one (it arrives empty). NOREPLACE by the library's own rule."""
    payload: dict = {"name": dest, "kind": doc["kind"], "definition": doc["definition"]}
    if doc["value"] is not None:
        payload["value"] = doc["value"]
    try:
        variables.create(payload)
    except session_input.AuthorityFenceBusy:
        raise store.StoreError("the variables library is busy; confirm again", status=503) from None
    except template_vars.VariableInUse:
        raise Terminal(f"the variables library refuses a new variable named {dest}") from None
    except template_vars.VariableError as exc:
        if "already exists" in str(exc):
            raise store.Conflict(str(exc)) from None
        raise store.StoreError(str(exc), status=422) from None
    except (
        template_vars.ResolutionUnavailable,
        template_vars.VariableStoreUnsupported,
        templates.TemplateInventoryIncomplete,
        template_secrets.RedactionUnavailable,
    ) as exc:
        raise store.StoreError(f"the variables library is unavailable: {exc}", status=503) from None
    current = variable_at(dest)
    if current is None:
        raise store.StoreError("the new variable can't be read back", status=503)
    return current


def variable_at(dest: str) -> str | None:
    rec = next((r for r in _variable_records() if r["name"] == dest), None)
    return None if rec is None else _variable_content(*_variable_view(rec))


def variable_kind(files: dict[str, bytes]) -> str | None:
    """The kind an incoming variable declares, `None` when it can't be read."""
    try:
        return _json_doc(files, VARIABLE_FILE).get("kind") if VARIABLE_FILE in files else None
    except Refused:
        return None


# ---- templates --------------------------------------------------------------------------------


def _template_records() -> list[dict]:
    """Every template, read strictly: a damaged library is never planned against or exported."""
    try:
        records, damaged = templates._read(templates.store_path())
    except templates.TemplateStoreUnsupported:
        raise store.StoreError(
            "the template library is from a newer BattleLab", status=503
        ) from None
    if damaged:
        raise store.StoreError("the template library can't be read in full", status=503)
    return records


def template_ids() -> dict[str, str | None]:
    # `updated_at` moves on every edit (a send doesn't touch it): what a preview binds.
    return {r["id"]: repr(r["updated_at"]) for r in _template_records()}


def _library_fields(doc: dict) -> list[dict]:
    return [f for f in doc["fields"] if f["source"] == "library"]


def _outgoing_images(rec: dict) -> list[dict]:
    """The images as they travel. An image given no name is named after its stored upload, whose
    basename the upload route sanitized (a lossy form of the original file name the exact scan
    can't see through): that default travels as `image <n>`, never the stored name. The picker
    judges exactly these names too."""
    return [
        {
            "name": f"image {n}" if img["name"] == Path(img["path"]).name else img["name"],
            "file": f"{IMAGES_DIR}/{n}{Path(img['path']).suffix.lower()}",
        }
        for n, img in enumerate(rec["images"], 1)
    ]


def template_export(tid: str, secrets: list[str], *, probe: bool = False) -> dict:
    """The template's file and images. `probe` (the export picker) checks each image's size and
    signature without reading it whole; the export itself always reads and checks every byte.

    The scan runs FIRST, before any check whose refusal could quote the template's own text (a
    field or image name): a refusal never echoes what the scan would have withheld."""
    rec = next((r for r in _template_records() if r["id"] == tid), None)
    if rec is None:
        raise store.NotFound(tid)
    doc = {k: rec[k] for k in ("name", "description", "tags", "body", "fields")}
    images = _outgoing_images(rec)
    scan(_template_texts({**doc, "id": tid, "images": images}), secrets)
    library = {r["name"]: r["kind"] for r in _variable_records()} if _library_fields(rec) else {}
    for f in _library_fields(rec):
        if library.get(f["name"]) != f["kind"]:
            raise Refused(
                f"its field {f['name']} names no {f['kind']} variable in your library; fix the "
                "template before exporting it"
            )
    files: dict[str, bytes] = {}
    for n, (img, out) in enumerate(zip(rec["images"], images, strict=True), 1):
        stored = Path(img["path"]).name
        suffix = Path(stored).suffix.lower()
        if probe:
            seen = _probe_upload(stored)
            if seen is not None and seen[0] > IMAGE_MAX_BYTES:
                raise Refused(
                    f"its image {n} is larger than {IMAGE_MAX_BYTES // (1024 * 1024)} MiB"
                )
            data = None if seen is None else seen[1]
        else:
            data = _read_upload(stored)
        if data is None:
            raise Refused(f"its image {n} is missing or can't be read; remove it first")
        if not _image_ok(suffix, data):
            raise Refused(f"its image {n} is not the picture its name says")
        files[out["file"]] = data
    files[TEMPLATE_FILE] = _json_bytes({"format": FILE_FORMAT, **doc, "images": images})
    return {
        "title": rec["name"],
        "files": files,
        "depends_on": [f"variable:{f['name']}" for f in _library_fields(rec)],
        # The local ID never travels: it was minted from the name the template had THEN (lower-
        # cased, so a secret typed in an old name survives there out of reach of the case-
        # sensitive scan). The file carries a slug of its CURRENT, scanned name instead.
        "bundle_id": templates._mint_id(rec["name"], set()),
    }


def template_doc(tid: str, files: dict[str, bytes]) -> dict:
    """An incoming template, normalized by the template store's own validator, or `Refused`."""
    if not isinstance(tid, str) or not templates.ID_RE.fullmatch(tid):
        raise Refused(f"{tid!r} is not a template ID")
    if TEMPLATE_FILE not in files:
        raise Refused(f"a template holds {TEMPLATE_FILE}")
    doc = _json_doc(files, TEMPLATE_FILE)
    if set(doc) != _TEMPLATE_KEYS:
        raise Refused(f"{TEMPLATE_FILE} doesn't have the fields of format 1")
    raw_images = doc["images"]
    if not isinstance(raw_images, list) or len(raw_images) > templates.IMAGES_MAX:
        raise Refused(f"a template has at most {templates.IMAGES_MAX} images")
    try:
        editable = templates.validate(
            {k: doc[k] for k in ("name", "description", "tags", "body", "fields")},
            check_files=False,
        )
    except templates.TemplateError as exc:
        raise Refused(f"not a valid template here: {exc}") from None
    images, used = [], set()
    for raw in raw_images:
        if not isinstance(raw, dict) or set(raw) != _IMAGE_KEYS:
            raise Refused("an image is {name, file}")
        path = raw["file"]
        if not isinstance(path, str) or path not in files or path == TEMPLATE_FILE or path in used:
            raise Refused("an image names a file the template doesn't carry")
        used.add(path)
        suffix = Path(path).suffix.lower()
        if (
            Path(path).parent != Path(IMAGES_DIR)
            or suffix not in templates.IMAGE_SUFFIXES
            or not _image_ok(suffix, files[path])
        ):
            raise Refused("an image must be a png, jpg, gif or webp picture")
        try:
            name = templates._text(raw, "name", max_len=templates.IMAGE_NAME_MAX, required=True)
        except templates.TemplateError as exc:
            raise Refused(f"not a valid template here: image {exc}") from None
        images.append({"name": name, "file": path})
    if set(files) != {TEMPLATE_FILE, *used}:
        raise Refused("it carries a file that isn't one of its images")
    return {**editable, "images": images}


def template_requires(tid: str, files: dict[str, bytes]) -> dict[str, str]:
    """`{variable name: field kind}` of every library field: what the template needs."""
    return {f["name"]: f["kind"] for f in _library_fields(template_doc(tid, files))}


def field_names(tid: str, files: dict[str, bytes]) -> set[str]:
    """Every name a renamed variable must not take for this template: its fields, and every
    `{{name}}` its body holds (a literal slot no field declares is sent as typed, and a rename
    onto it would fill it)."""
    try:
        doc = template_doc(tid, files)
    except Refused:
        return set()
    return {f["name"] for f in doc["fields"]} | set(FIELD_TOKEN_RE.findall(doc["body"]))


def template_rewrite(tid: str, files: dict[str, bytes], renames: dict[str, str]) -> dict:
    """The template with every renamed variable followed: its library field and every
    `{{name}}` slot of it in the body. Field names never collide: a renamed variable's new name
    is never one a bundled template declares (the preview plans it so)."""
    doc = template_doc(tid, files)
    moved = {
        f["name"]: renames[f["name"]]
        for f in _library_fields(doc)
        if renames.get(f["name"], f["name"]) != f["name"]
    }
    if not moved:
        return dict(files)
    if set(moved.values()) & field_names(tid, files):
        raise Refused("a renamed variable would clash with one of its own fields")
    raw = _json_doc(files, TEMPLATE_FILE)
    for field in raw["fields"]:
        if isinstance(field, dict) and field.get("name") in moved:
            field["name"] = moved[field["name"]]

    def one(m):
        return "{{" + moved[m.group(1)] + "}}" if m.group(1) in moved else m.group(0)

    raw["body"] = FIELD_TOKEN_RE.sub(one, raw["body"])
    out = dict(files)
    out[TEMPLATE_FILE] = _json_bytes(raw)
    template_doc(tid, out)  # still a valid template
    return out


def template_prepare(tid: str, files: dict[str, bytes]) -> tuple[tuple[dict, list], str]:
    doc = template_doc(tid, files)
    data = [files[i["file"]] for i in doc["images"]]
    return (doc, data), _template_content(doc, data)


def template_publish(dest: str, payload: tuple[dict, list]) -> str:
    """Write each image as a new upload, then create the template at exactly `dest` (NOREPLACE,
    full validation). A failed create removes the uploads it just wrote."""
    from fastapi import HTTPException

    from ..routes.upload import store_upload

    doc, data = payload
    written: list[str] = []
    images = []

    def discard() -> None:
        # Not while the template at `dest` uses them: a create can fail AFTER its replace (a
        # directory fsync), and the template it wrote must keep its pictures. A template someone
        # else put there uses none of these new files, so they go.
        try:
            rec = next((r for r in _template_records() if r["id"] == dest), None)
        except Exception:  # noqa: BLE001 — can't tell: keep the files (a stray upload is harmless)
            return
        if rec is not None and {Path(i["path"]).name for i in rec["images"]} & set(written):
            return
        _remove_uploads(written)

    try:
        for n, (img, body) in enumerate(zip(doc["images"], data, strict=True), 1):
            path, _safe = store_upload([body], f"imported-{n}{Path(img['file']).suffix.lower()}")
            written.append(path.name)
            images.append({"name": img["name"], "path": str(path)})
        payload_doc = {k: doc[k] for k in ("name", "description", "tags", "body", "fields")}
        created = templates.create_template({**payload_doc, "images": images}, tid=dest)
    except templates.TemplateIdTaken as exc:
        discard()
        raise store.Conflict(str(exc)) from None
    except templates.TemplateError as exc:
        discard()
        raise store.StoreError(str(exc), status=422) from None
    except HTTPException as exc:
        discard()
        raise store.StoreError(f"an image can't be stored: {exc.detail}", status=503) from None
    except session_input.AuthorityFenceBusy:
        discard()
        raise store.StoreError("the template library is busy; confirm again", status=503) from None
    except (templates.TemplateInventoryIncomplete, templates.TemplateStoreUnsupported) as exc:
        discard()
        raise store.StoreError(str(exc), status=503) from None
    except BaseException:
        discard()
        raise
    if created["id"] != dest:
        raise store.Conflict("the template was created under another ID")
    current = template_at(dest)
    if current is None:
        raise store.StoreError("the new template can't be read back", status=503)
    return current


def template_at(dest: str) -> str | None:
    rec = next((r for r in _template_records() if r["id"] == dest), None)
    if rec is None:
        return None
    data = [_read_upload(Path(i["path"]).name) for i in rec["images"]]
    return _template_content(rec, data)


# ---- the export picker ------------------------------------------------------------------------


def _judge(kind: str, rec: dict, secrets: list[str]) -> None:
    """Scan everything a picker row could show of this item (its ID or name first), or `Refused`.
    Runs on the record in hand, before anything else, so no row text is ever unjudged."""
    if kind == "variable":
        scan([("variable name", rec["name"])], secrets)
        _kind, definition, value = _variable_view(rec)
        texts = _definition_texts(definition)
        if value is not None:
            texts.append(("value", variable_types.text(value)))
        scan(texts, secrets)
        return
    doc = {k: rec[k] for k in ("name", "description", "tags", "body", "fields")}
    scan(_template_texts({**doc, "id": rec["id"], "images": _outgoing_images(rec)}), secrets)


def catalog() -> list[dict]:
    """Every template and global variable, each with whether it can be exported and why not.

    **One rule:** a row shows an item's own text (ID, name, title, dependencies) only when this
    call's scan judged it clean. When the scan can't run, every row is a placeholder."""
    try:
        secrets = secret_inventory()
        why_all = None
    except Refused as exc:
        secrets, why_all = [], str(exc)
    out: list[dict] = []
    try:
        vrecs = sorted(_variable_records(), key=lambda r: r["name"])
    except store.StoreError:
        vrecs = []
    try:
        trecs = templates._sorted(_template_records())
    except store.StoreError:
        trecs = []
    for kind, rec in [("variable", r) for r in vrecs] + [("template", r) for r in trecs]:
        row: dict = {
            "kind": kind,
            "id": rec["name"] if kind == "variable" else rec["id"],
            "title": rec["name"],
            "depends_on": [],
            "exportable": False,
            "reason": why_all,
            "source": "local",
            "note": None,
        }
        if kind == "variable":
            row["secret"] = rec["kind"] in template_vars.SECRET_KINDS
        if why_all is not None:
            _placeholder(row, out, "unavailable")
            out.append(row)
            continue
        try:
            _judge(kind, rec, secrets)
        except (Refused, variable_types.DefinitionError, ValueError, KeyError, TypeError) as exc:
            row["reason"] = _reason(exc)
            if getattr(exc, "withhold_id", False) or not getattr(exc, "scanned", False):
                _placeholder(row, out, "ID withheld")  # not even the ID was judged clean
            else:
                row["title"] = row["id"]  # the ID was judged clean; the rest of its text wasn't
            out.append(row)
            continue
        row["exportable"] = True
        try:
            if kind == "variable":
                row["title"] = variable_export(rec["name"], secrets)["title"]
                if rec["kind"] in template_vars.SECRET_KINDS:
                    row["note"] = "Definition only: its value never leaves this install."
                elif rec.get("unset"):
                    row["note"] = "No value is set: it travels as a definition."
            else:
                row["depends_on"] = [f"variable:{f['name']}" for f in _library_fields(rec)]
                template_export(rec["id"], secrets, probe=True)
        except (Refused, store.StoreError, variable_types.DefinitionError) as exc:
            _refuse_row(row, exc, out)
        out.append(row)
    return out


def _placeholder(row: dict, out: list[dict], why: str) -> None:
    """Replace a row's own text: `~` is in no store's ID shape, so it never collides."""
    n = sum(1 for r in out if str(r["id"]).startswith("~withheld-")) + 1
    row.update(
        id=f"~withheld-{n}",
        title=f"{row['kind'].capitalize()} ({why})",
        exportable=False,
        depends_on=[],
    )


# The `~withheld-<n>` numbers of the import marks and of the picker rows are independent: nothing
# joins a mark to a row by them.
def withhold_marks(marks: dict) -> dict:
    """The import marks with every template or variable key whose ID the scan can't judge clean
    replaced by a placeholder key (its value, the bundle digest, is kept: the import dialog counts
    copies per bundle). Fails closed: no inventory, no template or variable ID."""
    try:
        secrets: list[str] | None = secret_inventory()
    except Refused:
        secrets = None
    out: dict = {}
    n = 0
    for key, value in marks.items():
        kind, _, item_id = key.partition(":")
        if kind in ("template", "variable"):
            try:
                if secrets is None:
                    raise Refused("unjudged")
                scan([("ID", item_id)], secrets)
            except Refused:
                n += 1
                key = f"{kind}:~withheld-{n}"
        out[key] = value
    return out


def _refuse_row(row: dict, exc: Exception, out: list[dict]) -> None:
    """Mark a picker row unexportable. When its own ID or name is what holds a secret, the row
    carries a placeholder instead, so the catalog never echoes it (it can't be exported anyway)."""
    # An unexportable row is never chosen, so it names no dependency (a library field's name is
    # a variable's name, which may be what the refusal is about).
    row.update(exportable=False, reason=_reason(exc), depends_on=[])
    if getattr(exc, "withhold_id", False):
        _placeholder(row, out, "ID withheld")
    elif getattr(exc, "scanned", False):
        # The title is the item's own text (a template's name, a variable's label), which may be
        # what the scan found: the row shows the ID, which the scan judged clean.
        row["title"] = row["id"]


def _reason(exc: Exception) -> str:
    text = getattr(exc, "detail", None) or str(exc)
    return f"{text[:1].upper()}{text[1:]}."
