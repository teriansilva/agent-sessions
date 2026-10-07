"""Canonical signed plugin feeds and their immutable artifact recipes (#1259).

Signatures use the INSTALLED package's trust root, never keys supplied by the downloaded feed.
Parsing and signature verification are separate from acceptance: acceptance also serializes the
durable high-water mark, so racing refreshes cannot roll the feed backwards. No artifact runs here.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import kinds, provenance, runner
from .manifest import Manifest, load_bytes

MAX_BYTES = 2 * 1024 * 1024
MAX_PLUGINS = 64
MAX_ARTIFACTS = 1024
MAX_AGE = 7 * 86400
CLOCK_SKEW = 300
PRINCIPAL = "release@agent-sessions"
NAMESPACE = "battlelab-plugins"
SIGNERS = Path(__file__).with_name("feed-signers")
_SHA = re.compile(r"[0-9a-f]{64}")
_PART = re.compile(r"[A-Za-z0-9@_.+-]{1,200}")


class FeedError(ValueError):
    pass


def canonical(value: object) -> bytes:
    try:
        # ASCII escapes must not make an invalid Unicode surrogate look like a valid label/path.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (ValueError, TypeError, RecursionError):
        raise FeedError("the feed is not canonical JSON data") from None


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise FeedError("duplicate JSON field")
        out[key] = value
    return out


def _no_float(value: str):
    raise FeedError("numbers must be integers")


def decode(data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise FeedError("the feed is too large")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_no_float,
            parse_constant=_no_float,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise FeedError("the feed must be unambiguous UTF-8 JSON") from None
    if not isinstance(value, dict) or canonical(value) != data:
        raise FeedError("the feed bytes are not canonical")
    return value


def _fields(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise FeedError("unexpected or missing fields")
    return value


def relative(value: object, *, root: bool = False) -> str:
    if value == "." and root:
        return "."
    if not isinstance(value, str) or len(value) > 1024:
        raise FeedError("invalid relative destination")
    if any(p in (".", "..") or not _PART.fullmatch(p) for p in value.split("/")):
        raise FeedError("invalid relative destination")
    return value


def artifact_url(value: object, kind: str, *, redirect: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 for c in value):
        raise FeedError("invalid artifact URL")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise FeedError("invalid artifact URL") from None
    authorities = kinds.INSTALL_AUTHORITIES.get(kind, frozenset())
    if redirect and kind == "tarball":
        # GitHub's release redirect destination. No suffix/wildcard host matching.
        authorities = authorities | {"release-assets.githubusercontent.com"}
    if (
        url.scheme != "https"
        or url.hostname not in authorities
        or port not in (None, 443)
        or url.username is not None
        or url.password is not None
        or url.fragment
    ):
        raise FeedError("artifact authority is not allowed")
    if "\\" in value or any(p in (".", "..") for p in unquote(url.path).split("/")):
        raise FeedError("invalid artifact URL path")
    if not redirect:
        if url.query or not url.path.startswith("/"):
            raise FeedError("artifact URL must be immutable")
        if kind == "npm-prefix" and not re.fullmatch(
            r"/(?:@[a-z0-9._-]+/)?[a-z0-9._-]+/-/[A-Za-z0-9._+-]+\.tgz", url.path
        ):
            raise FeedError("expected an npm distribution URL")
        if kind == "tarball" and not re.fullmatch(
            r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[A-Za-z0-9_.+-]+/"
            r"[A-Za-z0-9_.+-]+\.(?:tar\.gz|tgz)",
            url.path,
        ):
            raise FeedError("expected a versioned GitHub release archive")
    return value


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str
    destination: str


@dataclass(frozen=True)
class Entry:
    manifest: Manifest
    manifest_bytes: bytes
    artifacts: tuple[Artifact, ...]
    digest: str


@dataclass(frozen=True)
class Feed:
    sequence: int
    issued_at: int
    expires_at: int
    entries: tuple[Entry, ...]
    digest: str


def entry(value: object, *, signed: bool) -> Entry:
    value = _fields(value, {"manifest", "recipe"})
    raw = canonical(value["manifest"])
    manifest = load_bytes(raw, name="plugin.json", source="signed-feed" if signed else "local")
    if manifest.runtime == "api":
        # The native runtime exists (#1278), but roster exposure waits for the custom UI slice
        # of #1273: until then no installed entry may present a client nothing can render.
        raise FeedError("native API adapters are not available in this build")
    if manifest.id in kinds.ROUTE_RESERVED_IDS:
        raise FeedError("this plugin id is reserved for a Settings route")
    if not signed and manifest.id in kinds.RESERVED_IDS:
        raise FeedError("a local manifest cannot replace a first-party agent")
    # Signed runtime data never inherits the in-tree exemption for shells/adopted binaries.
    provenance.check_vocabulary(manifest, provenance.LOCAL)
    if manifest.session_id.legacy_bare_id and manifest.id not in kinds.RESERVED_IDS:
        raise FeedError("only the in-tree identity may retain legacy bare IDs")
    recipe = _fields(value["recipe"], {"artifacts"})
    artifacts = recipe["artifacts"]
    install = manifest.install
    if install is None:
        if artifacts != [] or manifest.runtime != "chat":
            raise FeedError("a terminal agent needs an install recipe")
        return Entry(manifest, raw, (), hashlib.sha256(canonical(value)).hexdigest())
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_ARTIFACTS:
        raise FeedError("invalid artifact closure")
    out = []
    destinations = set()
    for item in artifacts:
        item = _fields(item, {"url", "sha256", "destination"})
        if not isinstance(item["sha256"], str) or not _SHA.fullmatch(item["sha256"]):
            raise FeedError("invalid artifact digest")
        destination = relative(item["destination"], root=install.kind == "tarball")
        if destination in destinations:
            raise FeedError("duplicate artifact destination")
        destinations.add(destination)
        if install.kind == "npm-prefix" and not destination.startswith("node_modules/"):
            raise FeedError("npm packages must be installed below node_modules")
        if install.kind == "npm-prefix":
            from . import npm_closure

            npm_closure.destination(destination)
        out.append(Artifact(artifact_url(item["url"], install.kind), item["sha256"], destination))
    if install.digest != "sha256:" + out[0].sha256:
        raise FeedError("the primary artifact does not match install.digest")
    if install.kind == "tarball":
        if len(out) != 1 or out[0].destination != ".":
            raise FeedError("tarball recipes contain one root archive")
        package = install.package.removeprefix("@")
        prefix = f"/{package}/releases/download/{install.version}/"
        if not urlsplit(out[0].url).path.startswith(prefix):
            raise FeedError("the archive does not match the manifest's package/version")
    else:
        package = install.package
        basename = package.rsplit("/", 1)[-1]
        expected = f"/{package}/-/{basename}-{install.version}.tgz"
        if urlsplit(out[0].url).path != expected or out[0].destination != f"node_modules/{package}":
            raise FeedError("the npm distribution does not match the manifest's package/version")
    return Entry(manifest, raw, tuple(out), hashlib.sha256(canonical(value)).hexdigest())


def parse(data: bytes, *, now: int | None = None, _historical: bool = False) -> Feed:
    doc = _fields(decode(data), {"contract", "sequence", "issued_at", "expires_at", "plugins"})
    now = int(time.time()) if now is None else now
    if type(doc["contract"]) is not int or doc["contract"] != 1:
        raise FeedError("unsupported plugin feed contract")
    if any(type(doc[k]) is not int or doc[k] <= 0 for k in ("sequence", "issued_at", "expires_at")):
        raise FeedError("invalid feed sequence or timestamp")
    issued, expires = doc["issued_at"], doc["expires_at"]
    if not issued < expires <= issued + MAX_AGE or (
        not _historical and (issued > now + CLOCK_SKEW or expires <= now)
    ):
        raise FeedError("the feed is expired or its validity interval is invalid")
    plugins = doc["plugins"]
    if not isinstance(plugins, list) or len(plugins) > MAX_PLUGINS:
        raise FeedError("invalid plugin list")
    entries = tuple(entry(p, signed=True) for p in plugins)
    if len({e.manifest.id for e in entries}) != len(entries):
        raise FeedError("duplicate plugin identity")
    return Feed(doc["sequence"], issued, expires, entries, hashlib.sha256(data).hexdigest())


def verify(data: bytes, signature: bytes) -> None:
    if not data or len(data) > MAX_BYTES or not 0 < len(signature) <= 16384:
        raise FeedError("invalid signature or feed size")
    with tempfile.TemporaryDirectory(prefix="battlelab-feed-") as temporary:
        sig = Path(temporary) / "feed.sig"
        sig.write_bytes(signature)
        result = runner.run(
            [
                "/usr/bin/ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(SIGNERS),
                "-I",
                PRINCIPAL,
                "-n",
                NAMESPACE,
                "-s",
                str(sig),
            ],
            data=data,
            timeout=10,
            max_output=16384,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
    if result.code != 0:
        raise FeedError("the feed signature is not from a trusted plugin-feed signer")


def accept(data: bytes, signature: bytes, *, now: int | None = None) -> Feed:
    from . import storage

    verify(data, signature)
    snapshot = parse(data, now=now)
    with storage.locked("feed.json") as path:
        old = storage.read(path)
        if old is not None:
            # An expired but intact old feed may advance. Corruption may not be repaired by
            # replacing the evidence with a newer feed, even if that new feed is valid.
            _stored(old, historical=True)
            if snapshot.sequence < old["sequence"]:
                raise FeedError("the feed sequence would go backwards")
            if snapshot.sequence == old["sequence"] and snapshot.digest != old["digest"]:
                raise FeedError("this sequence already identifies different feed bytes")
        storage.write(
            path,
            {
                "sequence": snapshot.sequence,
                "digest": snapshot.digest,
                "data": data.decode("ascii"),
                "signature": signature.decode("ascii"),
            },
        )
    return snapshot


def _stored(record: dict, *, now: int | None = None, historical: bool = False) -> Feed:
    try:
        if (
            set(record) != {"sequence", "digest", "data", "signature"}
            or type(record["sequence"]) is not int
        ):
            raise FeedError("the stored feed is malformed; left untouched")
        data, signature = record["data"].encode("ascii"), record["signature"].encode("ascii")
        verify(data, signature)
        snapshot = parse(data, now=now, _historical=historical)
        if snapshot.sequence != record["sequence"] or snapshot.digest != record["digest"]:
            raise FeedError("the stored feed does not match its high-water mark; left untouched")
        return snapshot
    except (KeyError, AttributeError, UnicodeError, TypeError):
        raise FeedError("the stored feed is malformed; left untouched") from None


def current(*, now: int | None = None) -> Feed | None:
    from . import storage

    with storage.locked("feed.json") as path:
        record = storage.read(path)
        return None if record is None else _stored(record, now=now)
