"""Gallery v1: bounded, canonical, inert text envelopes shared with BattleLab.

This module has no storage, network, execution or agent discovery dependencies. Keep
its bytes and contract vectors identical in publisher and consumer distributions.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

MAX_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 128
MAX_FILES = 64
MAX_FILE_BYTES = 128 * 1024
MAX_CONTENT_BYTES = 256 * 1024
MAX_AGE = 7 * 86400
CLOCK_SKEW = 300
NAMESPACE = "battlelab-gallery"
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)
_SHA = re.compile(r"[0-9a-f]{64}")


class GalleryError(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GalleryError("Duplicate JSON field.")
        result[key] = value
    return result


def _no_float(value):
    raise GalleryError("Numbers must be integers.")


def canonical(value: object) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        raw.encode("utf-8")  # Reject unpaired Unicode surrogates, even in escaped JSON.
        json.loads(raw, parse_float=_no_float, parse_constant=_no_float)
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GalleryError("Expected canonical JSON without floats or invalid Unicode.") from None


def decode(data: bytes) -> dict:
    if not data or len(data) > MAX_BYTES:
        raise GalleryError("Gallery exceeds its 2 MiB capacity.")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_no_float,
            parse_constant=_no_float,
        )
        if not isinstance(value, dict) or canonical(value) != data:
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise GalleryError("Expected exact canonical UTF-8 JSON bytes.") from None


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise GalleryError("Unexpected or missing gallery fields.")
    return value


def _text(value, label, limit, *, multiline=False):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise GalleryError(f"Invalid {label}.")
    if any((ord(c) < 32 and (not multiline or c not in "\t\n\r")) or ord(c) == 127 for c in value):
        raise GalleryError(f"Control character in {label}.")
    return value


def path(value):
    _text(value, "file path", 256)
    parts = value.split("/")
    if (
        len(parts) > 8
        or unicodedata.normalize("NFC", value) != value
        or "\\" in value
        or any(
            p in ("", ".", "..") or p.casefold() == ".git" or p.endswith((".", " ")) or ":" in p
            for p in parts
        )
    ):
        raise GalleryError(
            "File paths must be normalized relative paths without traversal or .git."
        )
    return value


def content(value: object) -> dict:
    doc = _fields(
        value, "format kind id version title author publisher summary source requirements files"
    )
    if (
        type(doc["format"]) is not int
        or doc["format"] != 1
        or doc["kind"] not in ("playbook", "skill")
    ):
        raise GalleryError("Unsupported content kind or format.")
    if not isinstance(doc["id"], str) or not _SLUG.fullmatch(doc["id"]):
        raise GalleryError("Expected a lowercase content ID of at most 64 characters.")
    version = _text(doc["version"], "version", 64)
    match = _VERSION.fullmatch(version)
    prerelease = version.split("+", 1)[0].split("-", 1)
    if (
        not match
        or (
            len(prerelease) == 2
            and any(
                not part or (part.isdigit() and len(part) > 1 and part.startswith("0"))
                for part in prerelease[1].split(".")
            )
        )
        or ("+" in version and any(not part for part in version.split("+", 1)[1].split(".")))
    ):
        raise GalleryError("Expected a semantic version.")
    for field, limit in (("title", 200), ("author", 200), ("publisher", 200), ("summary", 2000)):
        _text(doc[field], field, limit)
    source = _text(doc["source"], "source link", 2048)
    try:
        url = urlsplit(source)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.port not in (None, 443)
            or any(c.isspace() for c in source)
        ):
            raise ValueError()
    except ValueError:
        raise GalleryError("Source must be a public HTTPS link; it is never fetched.") from None
    requirements = doc["requirements"]
    if not isinstance(requirements, list) or len(requirements) > 32:
        raise GalleryError("Expected at most 32 requirements.")
    for item in requirements:
        _text(item, "requirement", 500)
    files = doc["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise GalleryError("Content needs between 1 and 64 text files.")
    seen, directories, spelling, total = set(), set(), {}, 0
    for name, text in files.items():
        name = path(name)
        folded = name.casefold()
        parents = ["/".join(name.split("/")[:n]).casefold() for n in range(1, len(name.split("/")))]
        if folded in seen or folded in directories or any(p in seen for p in parents):
            raise GalleryError("Case collision or file/directory path collision.")
        parts = name.split("/")
        for n in range(1, len(parts) + 1):
            segment = "/".join(parts[:n])
            if spelling.setdefault(segment.casefold(), segment) != segment:
                raise GalleryError("Case collision in a file or directory path.")
        seen.add(folded)
        directories.update(parents)
        if not isinstance(text, str):
            raise GalleryError("Files must contain UTF-8 text.")
        if any((ord(c) < 32 and c not in "\t\n\r") or ord(c) == 127 for c in text):
            raise GalleryError("Control character in file content.")
        size = len(text.encode("utf-8"))
        total += size
        if size > MAX_FILE_BYTES or total > MAX_CONTENT_BYTES:
            raise GalleryError("Content exceeds the 128 KiB file or 256 KiB total limit.")
        if name.endswith(".toml"):
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                raise GalleryError(f"Invalid TOML in {name}.") from None
    if doc["kind"] == "skill":
        if not files.get("SKILL.md", "").strip():
            raise GalleryError("Skills require a nonempty SKILL.md.")
    else:
        if "playbook.toml" not in files:
            raise GalleryError("Playbooks require playbook.toml.")
        manifest = tomllib.loads(files["playbook.toml"])
        identity = manifest.get("identity", {})
        expected = {
            "id": doc["id"],
            "version": doc["version"],
            "name": doc["title"],
            "publisher": doc["publisher"],
            "summary": doc["summary"],
        }
        if not isinstance(identity, dict) or any(identity.get(k) != v for k, v in expected.items()):
            raise GalleryError("Playbook identity must agree with its content envelope.")
    return doc


def entry(value):
    doc = _fields(value, "content digest available")
    content(doc["content"])
    digest = hashlib.sha256(canonical(doc["content"])).hexdigest()
    if (
        type(doc["available"]) is not bool
        or not isinstance(doc["digest"], str)
        or not _SHA.fullmatch(doc["digest"])
        or doc["digest"] != digest
    ):
        raise GalleryError("Invalid availability or content digest.")
    return doc


def identity(entry):
    doc = entry["content"]
    return doc["kind"], doc["id"], doc["version"]


@dataclass(frozen=True)
class Gallery:
    sequence: int
    issued_at: int
    expires_at: int
    entries: tuple[dict, ...]
    digest: str


def parse(data: bytes, *, now: int | None = None, _historical=False) -> Gallery:
    doc = _fields(decode(data), "format sequence issued_at expires_at entries")
    if type(doc["format"]) is not int or doc["format"] != 1:
        raise GalleryError("Unsupported gallery format.")
    if any(
        type(doc[k]) is not int or not 0 < doc[k] <= 9999999999999999
        for k in ("sequence", "issued_at", "expires_at")
    ):
        raise GalleryError("Invalid sequence or timestamp.")
    now = int(time.time()) if now is None else now
    issued, expires = doc["issued_at"], doc["expires_at"]
    if not issued < expires <= issued + MAX_AGE or (
        not _historical and (issued > now + CLOCK_SKEW or expires <= now)
    ):
        raise GalleryError("Gallery is expired or its validity interval is invalid.")
    if not isinstance(doc["entries"], list) or len(doc["entries"]) > MAX_ENTRIES:
        raise GalleryError("Gallery exceeds its 128-entry capacity.")
    entries = tuple(entry(e) for e in doc["entries"])
    if len({identity(e) for e in entries}) != len(entries):
        raise GalleryError("Duplicate kind, ID and version.")
    return Gallery(doc["sequence"], issued, expires, entries, hashlib.sha256(data).hexdigest())


def successor(previous: Gallery, following: Gallery):
    """Withdrawal keeps bytes. No version rewrite or capacity-driven history eviction."""
    current = {identity(e): e for e in following.entries}
    for old in previous.entries:
        newer = current.get(identity(old))
        if newer is None or newer["digest"] != old["digest"]:
            raise GalleryError(
                "Retain every published version unchanged; withdraw it by availability only."
            )
