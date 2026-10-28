"""The connector manifest: a strict TOML validator (#1746 D1, #1750).

A manifest DESCRIBES a connector and CHOOSES its behaviour from reviewed code; it can never add
any. So the validator is strict at every level:

* an unknown field, at the top or in any table, refuses the whole manifest (an unknown field in a
  newer contract may be a restriction, and ignoring a restriction widens what it can do);
* every enumerated value must be a member of a set in :mod:`.kinds`;
* ``contract`` must be one this build reads (`kinds.CONTRACT_CURRENT`);
* free text (label, setup note) is bounded and printable.

The refusal vectors are pinned in ``tests/fixtures/connectors/``. The bundled manifests ship as
package data (``connectors/first_party/*.toml``) and are read back from the installed package.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from urllib.parse import urlsplit

from . import kinds

ID_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")
VERSION_RE = re.compile(r"[0-9]{1,6}")
LABEL_MAX = 40
SETUP_MAX = 400
MANIFEST_MAX_BYTES = 16 * 1024

_TOP = frozenset({"contract", "identity", "adapter", "origin", "auth", "capabilities", "docs"})
_REQUIRED_TOP = frozenset({"contract", "identity", "adapter", "origin", "auth", "capabilities"})
_TABLES = {
    "identity": (frozenset({"id", "label", "publisher", "version", "category"}), None),
    "adapter": (frozenset({"kind"}), None),
    "origin": (frozenset({"mode"}), frozenset({"fixed"})),
    "auth": (frozenset({"kind", "scheme", "scopes"}), None),
    "capabilities": (frozenset({"read"}), None),
    "docs": (frozenset(), frozenset({"setup"})),
}


class ManifestError(ValueError):
    """The manifest is refused. The message names the field, never echoes a long value."""


@dataclass(frozen=True)
class Manifest:
    id: str
    label: str
    publisher: str
    version: str
    category: str
    adapter: str
    origin_mode: str
    fixed_origin: str | None
    auth_kind: str
    auth_scheme: str
    scopes: tuple[str, ...]
    reads: tuple[str, ...]
    setup: str

    def public(self) -> dict:
        """The catalog view (no secret exists in a manifest; this is its whole description)."""
        return {
            "id": self.id,
            "label": self.label,
            "publisher": self.publisher,
            "version": self.version,
            "category": self.category,
            "adapter": self.adapter,
            "origin_mode": self.origin_mode,
            "fixed_origin": self.fixed_origin,
            "auth": {"kind": self.auth_kind, "scopes": list(self.scopes)},
            "capabilities": {"read": list(self.reads)},
            "setup": self.setup,
        }


def _text(value: object, field: str, cap: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    if len(value) > cap:
        raise ManifestError(f"{field} is longer than {cap} characters")
    if not value.isprintable():
        raise ManifestError(f"{field} must be printable text")
    return value.strip()


def _member(value: object, field: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ManifestError(f"{field} must be one of {sorted(allowed)}")
    return value


def _members(value: object, field: str, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ManifestError(f"{field} must be a non-empty list")
    out = tuple(_member(v, field, allowed) for v in value)
    if len(set(out)) != len(out):
        raise ManifestError(f"{field} lists a value twice")
    return out


def _fixed_origin(value: object) -> str:
    if not isinstance(value, str):
        raise ManifestError("origin.fixed must be a string")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ManifestError("origin.fixed is not a URL") from None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ManifestError("origin.fixed must be an https origin without credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ManifestError("origin.fixed must be an origin, without a path")
    return f"https://{parts.netloc.lower()}"


def validate(doc: object) -> Manifest:
    """A parsed TOML document → `Manifest`, or `ManifestError`."""
    if not isinstance(doc, dict):
        raise ManifestError("a manifest is a TOML table")
    unknown = set(doc) - _TOP
    if unknown:
        raise ManifestError(f"unknown field {sorted(unknown)[0]!r}")
    missing = _REQUIRED_TOP - set(doc)
    if missing:
        raise ManifestError(f"missing {sorted(missing)[0]!r}")
    contract = doc["contract"]
    if not isinstance(contract, int) or isinstance(contract, bool):
        raise ManifestError("contract must be an integer")
    if contract > kinds.CONTRACT_CURRENT:
        raise ManifestError("this connector needs a newer BattleLab (contract too new)")
    if contract != kinds.CONTRACT_CURRENT:
        raise ManifestError("contract is not one this build reads")
    tables: dict[str, dict] = {}
    for name, (required, optional) in _TABLES.items():
        t = doc.get(name, {} if name == "docs" else None)
        if not isinstance(t, dict):
            raise ManifestError(f"{name} must be a table")
        allowed = required | (optional or frozenset())
        extra = set(t) - allowed
        if extra:
            raise ManifestError(f"unknown field {name}.{sorted(extra)[0]}")
        absent = required - set(t)
        if absent:
            raise ManifestError(f"missing {name}.{sorted(absent)[0]}")
        tables[name] = t

    ident = tables["identity"]
    mid = ident["id"]
    if not isinstance(mid, str) or not ID_RE.fullmatch(mid):
        raise ManifestError("identity.id must be a lowercase id")
    version = ident["version"]
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ManifestError("identity.version must be a number in a string")
    adapter = _member(tables["adapter"]["kind"], "adapter.kind", kinds.ADAPTER_KINDS)

    origin = tables["origin"]
    mode = _member(origin["mode"], "origin.mode", kinds.ORIGIN_MODES)
    fixed: str | None = None
    if mode == "fixed":
        if "fixed" not in origin:
            raise ManifestError("origin.fixed is required when origin.mode is fixed")
        fixed = _fixed_origin(origin["fixed"])
    elif "fixed" in origin:
        raise ManifestError("origin.fixed is only allowed when origin.mode is fixed")

    auth = tables["auth"]
    auth_kind = _member(auth["kind"], "auth.kind", kinds.AUTH_KINDS)
    if auth_kind not in kinds.ADAPTER_AUTH[adapter]:
        raise ManifestError(f"auth.kind {auth_kind!r} is not one {adapter} speaks")
    scheme = _member(auth["scheme"], "auth.scheme", kinds.AUTH_SCHEMES)
    scopes = _members(auth["scopes"], "auth.scopes", kinds.AUTH_SCOPES)
    reads = _members(tables["capabilities"]["read"], "capabilities.read", kinds.READ_OPS)
    beyond = set(reads) - kinds.ADAPTER_READS[adapter]
    if beyond:
        raise ManifestError(f"capabilities.read {sorted(beyond)[0]!r} is not one {adapter} does")
    setup = tables["docs"].get("setup")
    return Manifest(
        id=mid,
        label=_text(ident["label"], "identity.label", LABEL_MAX),
        publisher=_member(ident["publisher"], "identity.publisher", kinds.PUBLISHERS),
        version=version,
        category=_member(ident["category"], "identity.category", kinds.CATEGORIES),
        adapter=adapter,
        origin_mode=mode,
        fixed_origin=fixed,
        auth_kind=auth_kind,
        auth_scheme=scheme,
        scopes=scopes,
        reads=reads,
        setup="" if setup is None else _text(setup, "docs.setup", SETUP_MAX),
    )


def parse(data: bytes) -> Manifest:
    """Manifest bytes → `Manifest`, or `ManifestError`."""
    if len(data) > MANIFEST_MAX_BYTES:
        raise ManifestError("the manifest is too large")
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(f"not valid TOML ({type(e).__name__})") from None
    return validate(doc)


def bundled() -> dict[str, Manifest]:
    """The first-party manifests shipped in the package, by id. A broken bundled manifest is a
    release defect and raises (the deploy-shape test reads them from the installed package)."""
    out: dict[str, Manifest] = {}
    root = resources.files(__package__).joinpath("first_party")
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".toml"):
            continue
        m = parse(entry.read_bytes())
        if m.id in out or f"{m.id}.toml" != entry.name:
            raise ManifestError(f"bundled manifest {entry.name} is misnamed or duplicated")
        out[m.id] = m
    return out
