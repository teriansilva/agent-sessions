"""Share bundle format 1 (#1714): one uncompressed ustar file with an exact manifest inventory.

A bundle is untrusted input. `read` turns its bytes into verified items or raises `Refused`,
and nothing is staged, parsed per kind or written before it returns:

* **ustar only.** POSIX `ustar\\0` + `00` headers with a valid checksum. Every member is a regular
  file (typeflag `0`). Links, devices, FIFOs, directories, PAX/GNU extension headers and
  base-256 sizes refuse the whole file. There is no compression, so no decompression bomb.
* **Bounded before reading.** The whole file, the member count, each member and the manifest
  have hard limits, checked from the header before the member's bytes are touched.
* **One spelling per path.** A name is valid UTF-8 in NFC, with no `./`, `..`, `//`,
  backslash, control character, leading or trailing `/`. Two names that fold to the same
  NFKC-casefolded string refuse (case-only and normalization-only collisions).
* **The manifest is an exact inventory.** Archive members and declared files match one to one,
  byte for byte in name: a duplicate member, an undeclared member and a missing declared file
  each refuse. Every file's size and sha256, every item's digest and every item's revision (the
  tree digest the stores use) are verified.
* **Versioned.** `format` is an integer. A newer major format is refused with its own reason
  (`newer_format`), never half-read. Unknown manifest keys refuse: format 1 is exact.

The writer emits the same shape deterministically (sorted members, fixed metadata).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field

from ..playbooks.tree import Tree

FORMAT = 1
SUFFIX = ".battlelab"
MANIFEST = "manifest.json"
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 2048
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ITEMS = 64
MAX_NAME_BYTES = 255
MAX_DEPTH = 12
TITLE_MAX = 200
SOURCE_MAX = 80
KIND_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
ITEM_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HEX = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BLOCK = 512
_MANIFEST_KEYS = frozenset({"format", "created_at", "app_version", "source", "items"})
_ITEM_KEYS = frozenset({"kind", "id", "title", "revision", "digest", "depends_on", "files"})
_FILE_KEYS = frozenset({"path", "size", "sha256"})


class Refused(ValueError):
    """The file is not a bundle this install can read. Nothing was staged or written."""

    def __init__(self, detail: str, reason: str = "invalid") -> None:
        super().__init__(detail)
        self.detail = detail
        self.reason = reason


@dataclass
class Item:
    kind: str
    id: str
    title: str
    revision: str
    digest: str
    depends_on: tuple[str, ...]
    files: dict[str, bytes]
    index: int = 0

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass
class Bundle:
    digest: str
    size: int
    created_at: str
    app_version: str
    source: str | None
    items: list[Item] = field(default_factory=list)

    def item(self, key: str) -> Item:
        for item in self.items:
            if item.key == key:
                return item
        raise KeyError(key)


def shown(name: object) -> str:
    """A hostile name, escaped and shortened, for a refusal message."""
    text = json.dumps(name if isinstance(name, str) else repr(name), ensure_ascii=True)
    return text if len(text) <= 120 else text[:117] + "…"


def tree_of(files: dict[str, bytes]) -> Tree:
    """The stores' snapshot shape: these files, with exactly their parents as directories."""
    dirs = set()
    for path in files:
        parts = path.split("/")
        dirs.update("/".join(parts[:n]) for n in range(1, len(parts)))
    return Tree(dict(files), dirs)


def tree_digest(files: dict[str, bytes]) -> str:
    """The revision the skills library and the playbook store compute for the same tree."""
    return tree_of(files).digest()


def item_digest(kind: str, item_id: str, files: dict[str, bytes]) -> str:
    """The item's content digest: kind, id and every file's path, size and sha256, framed."""
    h = hashlib.sha256(b"battlelab-share-item/1\0")
    h.update(kind.encode() + b"\0" + item_id.encode() + b"\0")
    for path in sorted(files):
        data = files[path]
        h.update(path.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0")
        h.update(hashlib.sha256(data).hexdigest().encode() + b"\0")
    return h.hexdigest()


def check_path(name: object, where: str = "path") -> str:
    """One spelling per relative path, or `Refused` naming the rule."""
    if not isinstance(name, str) or not name:
        raise Refused(f"{where}: a path is a non-empty string")
    if len(name.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES:
        raise Refused(f"{where}: {shown(name)} is longer than {MAX_NAME_BYTES} bytes")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise Refused(f"{where}: {shown(name)} is not valid Unicode") from None
    if unicodedata.normalize("NFC", name) != name:
        raise Refused(f"{where}: {shown(name)} is not in Unicode normal form C")
    if _CONTROL.search(name):
        raise Refused(f"{where}: {shown(name)} contains a control character")
    if "\\" in name:
        raise Refused(f"{where}: {shown(name)} contains a backslash")
    if name.startswith("/"):
        raise Refused(f"{where}: {shown(name)} is an absolute path")
    if name.endswith("/"):
        raise Refused(f"{where}: {shown(name)} ends with a slash")
    if "//" in name:
        raise Refused(f"{where}: {shown(name)} contains an empty path segment")
    parts = name.split("/")
    if any(part in (".", "..") for part in parts):
        raise Refused(f"{where}: {shown(name)} contains a . or .. segment")
    if len(parts) > MAX_DEPTH:
        raise Refused(f"{where}: {shown(name)} is nested deeper than {MAX_DEPTH}")
    return name


def _fold(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


# ---- writer ---------------------------------------------------------------------------------


def _octal(value: int, width: int) -> bytes:
    return f"{value:0{width - 1}o}".encode("ascii") + b"\0"


def _split_name(name: str) -> tuple[bytes, bytes]:
    raw = name.encode("utf-8")
    if len(raw) <= 100:
        return raw, b""
    for i in range(len(raw) - 1, -1, -1):
        if raw[i : i + 1] == b"/" and i <= 155 and len(raw) - i - 1 <= 100 and i > 0:
            return raw[i + 1 :], raw[:i]
    raise Refused(f"{shown(name)} cannot be stored in a ustar header")


def _header(name: str, size: int) -> bytes:
    short, prefix = _split_name(name)
    block = bytearray(_BLOCK)
    block[0 : len(short)] = short
    block[100:108] = b"0000644\0"
    block[108:116] = b"0000000\0"
    block[116:124] = b"0000000\0"
    block[124:136] = _octal(size, 12)
    block[136:148] = _octal(0, 12)
    block[148:156] = b" " * 8
    block[156:157] = b"0"
    block[257:263] = b"ustar\0"
    block[263:265] = b"00"
    block[345 : 345 + len(prefix)] = prefix
    checksum = sum(block)
    block[148:156] = f"{checksum:06o}".encode("ascii") + b"\0 "
    return bytes(block)


def _member(name: str, data: bytes) -> bytes:
    pad = (-len(data)) % _BLOCK
    return _header(name, len(data)) + data + b"\0" * pad


def write(
    items: list[dict], *, created_at: str, app_version: str, source: str | None = None
) -> bytes:
    """A format-1 bundle. Each item is `{kind, id, title, files: {path: bytes}, depends_on}`;
    revision and digests are computed here, so the manifest always matches the members."""
    manifest_items = []
    members: list[tuple[str, bytes]] = []
    for item in items:
        files = item["files"]
        entries = []
        for path in sorted(files):
            check_path(path)
            data = files[path]
            name = f"{item['kind']}/{item['id']}/{path}"
            check_path(name)
            _split_name(name)
            if len(data) > MAX_FILE_BYTES:
                raise Refused(f"{shown(name)} is larger than {MAX_FILE_BYTES} bytes")
            entries.append(
                {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            )
            members.append((name, data))
        manifest_items.append(
            {
                "kind": item["kind"],
                "id": item["id"],
                "title": item["title"],
                "revision": tree_digest(files),
                "digest": item_digest(item["kind"], item["id"], files),
                "depends_on": list(item.get("depends_on", ())),
                "files": entries,
            }
        )
    manifest: dict = {
        "format": FORMAT,
        "created_at": created_at,
        "app_version": app_version,
        "items": manifest_items,
    }
    if source:
        manifest["source"] = source
    raw = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise Refused("the manifest is larger than its limit; export fewer items")
    if len(members) + 1 > MAX_MEMBERS:
        raise Refused(f"a bundle holds at most {MAX_MEMBERS} files; export fewer items")
    out = bytearray(_member(MANIFEST, raw))
    for name, data in members:
        out += _member(name, data)
    out += b"\0" * (_BLOCK * 2)
    if len(out) > MAX_BUNDLE_BYTES:
        raise Refused(
            f"the bundle would be larger than {MAX_BUNDLE_BYTES} bytes; export fewer items"
        )
    return bytes(out)


# ---- reader ---------------------------------------------------------------------------------


def _field(block: bytes, start: int, end: int, what: str) -> bytes:
    raw = block[start:end]
    value, sep, rest = raw.partition(b"\0")
    if sep and rest.strip(b"\0"):
        raise Refused(f"a member header's {what} has data after its end")
    return value


def _number(block: bytes, start: int, end: int, what: str) -> int:
    raw = block[start:end]
    if raw[:1] and raw[0] & 0x80:
        raise Refused(f"a member header uses a base-256 {what}")
    text = raw.rstrip(b"\0 ").lstrip(b" ")
    if not text:
        return 0
    if not re.fullmatch(rb"[0-7]+", text):
        raise Refused(f"a member header's {what} is not an octal number")
    return int(text, 8)


_TYPES = {
    b"1": "a hard link",
    b"2": "a symbolic link",
    b"3": "a character device",
    b"4": "a block device",
    b"5": "a directory",
    b"6": "a FIFO",
    b"7": "a contiguous file",
    b"x": "a PAX extended header",
    b"g": "a PAX global header",
    b"L": "a GNU long name",
    b"K": "a GNU long link",
}


def _members(data: bytes) -> list[tuple[str, bytes]]:
    if len(data) > MAX_BUNDLE_BYTES:
        raise Refused(f"the file is larger than {MAX_BUNDLE_BYTES} bytes", "too_large")
    if not data or len(data) % _BLOCK:
        raise Refused("this is not a BattleLab bundle (not a ustar archive)")
    out: list[tuple[str, bytes]] = []
    offset = 0
    while True:
        if offset + _BLOCK > len(data):
            raise Refused("the archive is truncated (no end-of-archive marker)")
        block = data[offset : offset + _BLOCK]
        if not any(block):
            if any(data[offset:]):
                raise Refused("the archive has data after its end-of-archive marker")
            if len(data) - offset < 2 * _BLOCK:
                raise Refused("the archive is truncated (no end-of-archive marker)")
            return out
        if block[257:263] != b"ustar\0" or block[263:265] != b"00":
            raise Refused("this is not a BattleLab bundle (only POSIX ustar is accepted)")
        stored = _number(block, 148, 156, "checksum")
        if stored != sum(block[:148]) + 8 * 32 + sum(block[156:]):
            raise Refused("a member header's checksum does not match")
        typeflag = block[156:157]
        if typeflag not in (b"0", b"\0"):
            what = _TYPES.get(typeflag, f"an unsupported member type {typeflag!r}")
            raise Refused(f"the archive contains {what}; only regular files are allowed")
        if any(block[157:257]):
            raise Refused("a regular file member names a link target")
        name_raw = _field(block, 0, 100, "name")
        prefix_raw = _field(block, 345, 500, "prefix")
        raw = prefix_raw + b"/" + name_raw if prefix_raw else name_raw
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused(f"a member name is not valid UTF-8: {shown(raw)}") from None
        check_path(name, "member")
        size = _number(block, 124, 136, "size")
        limit = MAX_MANIFEST_BYTES if name == MANIFEST else MAX_FILE_BYTES
        if size > limit:
            raise Refused(f"{shown(name)} is larger than {limit} bytes", "too_large")
        if len(out) >= MAX_MEMBERS:
            raise Refused(f"the archive holds more than {MAX_MEMBERS} files", "too_large")
        start = offset + _BLOCK
        end = start + size
        if end > len(data):
            raise Refused("the archive is truncated (a member is cut short)")
        out.append((name, data[start:end]))
        offset = start + size + ((-size) % _BLOCK)


def _pairs(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise Refused(f"the manifest repeats the key {shown(key)}")
        seen[key] = value
    return seen


def _no_float(value):
    raise Refused("the manifest contains a number this format does not use")


def _text(value: object, where: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise Refused(f"manifest {where} is a non-empty string")
    if len(value) > limit or _CONTROL.search(value):
        raise Refused(
            f"manifest {where} is longer than {limit} characters or has a control character"
        )
    if not _utf8(value):
        raise Refused(f"manifest {where} is not valid Unicode text")
    return value


def _utf8(value: str) -> bool:
    """A lone surrogate survives JSON decoding but can't be answered or written as UTF-8."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _int(value: object, where: str) -> int:
    if type(value) is not int or value < 0:
        raise Refused(f"manifest {where} is a non-negative integer")
    return value


def _manifest(raw: bytes) -> dict:
    try:
        doc = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_no_float,
            parse_constant=_no_float,
        )
    except Refused:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Refused("manifest.json is not valid JSON") from None
    if not isinstance(doc, dict):
        raise Refused("manifest.json is not an object")
    fmt = doc.get("format")
    if type(fmt) is int and fmt > FORMAT:
        raise Refused(
            f"this bundle is format {fmt}; this install reads format {FORMAT}. "
            "Update BattleLab, then import it again.",
            "newer_format",
        )
    if type(fmt) is not int or fmt != FORMAT:
        raise Refused("manifest.json has no supported format number")
    unknown = sorted(set(doc) - _MANIFEST_KEYS)
    if unknown:
        raise Refused(f"manifest.json has unknown keys: {', '.join(map(shown, unknown))}")
    missing = sorted(_MANIFEST_KEYS - {"source"} - set(doc))
    if missing:
        raise Refused(f"manifest.json is missing {', '.join(missing)}")
    return doc


def _cycles(items: list[Item]) -> None:
    graph = {item.key: item.depends_on for item in items}
    state: dict[str, int] = {}

    def visit(key: str) -> None:
        state[key] = 1
        for dep in graph[key]:
            if state.get(dep) == 1:
                raise Refused(f"the items' dependencies form a cycle through {shown(dep)}")
            if dep not in state:
                visit(dep)
        state[key] = 2

    for key in graph:
        if key not in state:
            visit(key)


def read(data: bytes) -> Bundle:
    """Verify a bundle completely, or raise `Refused`. See the module docstring."""
    members = _members(data)
    if not members or members[0][0] != MANIFEST:
        raise Refused("this is not a BattleLab bundle (manifest.json must come first)")
    names: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    for name, body in members:
        if name in names:
            raise Refused(f"the archive repeats the member {shown(name)}")
        other = folded.get(_fold(name))
        if other is not None:
            raise Refused(
                f"the members {shown(other)} and {shown(name)} differ only by case or "
                "Unicode normalization"
            )
        names[name] = body
        folded[_fold(name)] = name
    doc = _manifest(names.pop(MANIFEST))
    raw_items = doc["items"]
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_ITEMS:
        raise Refused(f"manifest items is a list of 1 to {MAX_ITEMS} items")
    items: list[Item] = []
    keys: set[str] = set()
    declared: set[str] = set()
    for index, raw in enumerate(raw_items):
        where = f"items[{index}]"
        if not isinstance(raw, dict) or set(raw) != _ITEM_KEYS:
            raise Refused(f"manifest {where} must have exactly {', '.join(sorted(_ITEM_KEYS))}")
        kind, item_id = raw["kind"], raw["id"]
        if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
            raise Refused(f"manifest {where}.kind is not a kind name")
        if not isinstance(item_id, str) or not ITEM_ID_RE.fullmatch(item_id):
            raise Refused(f"manifest {where}.id is not an item id")
        key = f"{kind}:{item_id}"
        if key in keys:
            raise Refused(f"the manifest lists the item {shown(key)} twice")
        keys.add(key)
        title = _text(raw["title"], f"{where}.title", TITLE_MAX)
        for name in ("revision", "digest"):
            if not isinstance(raw[name], str) or not _HEX.fullmatch(raw[name]):
                raise Refused(f"manifest {where}.{name} is a sha256 digest")
        deps = raw["depends_on"]
        if (
            not isinstance(deps, list)
            or len(deps) > MAX_ITEMS
            or not all(isinstance(d, str) for d in deps)
            or len(set(deps)) != len(deps)
        ):
            raise Refused(f"manifest {where}.depends_on is a list of distinct item keys")
        entries = raw["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_MEMBERS:
            raise Refused(f"manifest {where}.files lists 1 to {MAX_MEMBERS} files")
        files: dict[str, bytes] = {}
        for n, entry in enumerate(entries):
            fwhere = f"{where}.files[{n}]"
            if not isinstance(entry, dict) or set(entry) != _FILE_KEYS:
                raise Refused(f"manifest {fwhere} must have exactly path, size and sha256")
            path = check_path(entry["path"], fwhere)
            size = _int(entry["size"], f"{fwhere}.size")
            if not isinstance(entry["sha256"], str) or not _HEX.fullmatch(entry["sha256"]):
                raise Refused(f"manifest {fwhere}.sha256 is a sha256 digest")
            member = check_path(f"{kind}/{item_id}/{path}", fwhere)
            if member in declared or path in files:
                raise Refused(f"the manifest declares {shown(member)} twice")
            declared.add(member)
            if member not in names:
                raise Refused(f"the manifest declares {shown(member)}, which the archive lacks")
            body = names[member]
            if len(body) != size or hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise Refused(f"{shown(member)} does not match its digest in the manifest")
            files[path] = body
        clash = sorted(set(files) & tree_of(files).dirs)
        if clash:
            raise Refused(f"{shown(f'{key}/{clash[0]}')} is both a file and a folder")
        if item_digest(kind, item_id, files) != raw["digest"]:
            raise Refused(f"the item {shown(key)} does not match its digest")
        if tree_digest(files) != raw["revision"]:
            raise Refused(f"the item {shown(key)} does not match its revision")
        items.append(
            Item(kind, item_id, title, raw["revision"], raw["digest"], tuple(deps), files, index)
        )
    undeclared = sorted(set(names) - declared)
    if undeclared:
        raise Refused(
            f"the archive contains {shown(undeclared[0])}, which the manifest does not declare"
        )
    for item in items:
        for dep in item.depends_on:
            if dep == item.key or dep not in keys:
                raise Refused(
                    f"the item {shown(item.key)} depends on {shown(dep)}, "
                    "which is not in the bundle"
                )
    _cycles(items)
    source = doc.get("source")
    return Bundle(
        digest=hashlib.sha256(data).hexdigest(),
        size=len(data),
        created_at=_text(doc["created_at"], "created_at", 40),
        app_version=_text(doc["app_version"], "app_version", 64),
        source=None if source is None else _text(source, "source", SOURCE_MAX),
        items=items,
    )
