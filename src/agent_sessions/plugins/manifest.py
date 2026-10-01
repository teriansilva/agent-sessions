"""The versioned plugin manifest: schema, parser and validator (#853 P1).

A manifest is DATA. It selects built-in kinds (`kinds.py`) and supplies parameters those kinds
validate; it never carries code, a command string, a free-form flag, a path glob, a URL authority
outside an allowlist, or a regular expression outside the tiny grammar below.

**Unknown-field policy: reject.** An unknown key at any level makes the whole manifest invalid.
Ignoring it would be wrong in the direction that matters: a key added by a later contract may be a
*restriction*, and a reader that skips it grants what the author meant to withhold.

**Contract policy.** `contract` is an integer. A manifest declaring a contract this build does not
know is refused — higher: "needs a newer BattleLab"; lower or missing: invalid. When contract 2
exists, `MIGRATIONS[1]` rewrites a contract-1 document into contract-2 shape before validation, so
an old manifest keeps loading on a new app. The reverse is deliberately impossible: rolling the app
back disables a newer plugin with a stated reason instead of misreading it. An ADDITIVE optional
field (`runtime`, `launch.model`, `instructions`) stays on the current contract: an older build
refuses it as an unknown field, which is the same outcome (`kinds.CONTRACT_CURRENT`).

Parse failures raise `ManifestError` naming the field, so one bad manifest reports what is wrong
with it and the loader (fail-soft per plugin) carries on with the rest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import kinds

MAX_MANIFEST_BYTES = 64 * 1024
MAX_NATIVE_ID_LEN = 128

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
_ENV_BIN_RE = re.compile(r"^AGENT_SESSIONS_[A-Z0-9]{1,32}_BIN$")
_ENV_DIR_RE = re.compile(r"^AGENT_SESSIONS_[A-Z0-9_]{1,48}$")
_BIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BADGE_RE = re.compile(r"^[a-z]{2,3}$")
_PREFIX_RE = re.compile(r"^[a-z]{1,16}_$")
# A model id reaches argv (#1189) as the value after a `MODEL_FLAGS` flag: ASCII only, no space, no
# `=`, and a first character that cannot start an option. `\Z`, never `$`, so a trailing newline
# cannot ride through a `.match`/`.fullmatch` caller that forgets the difference.
_MODEL_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}\Z")
#: The picker's "no flag" choice. Never a model id or an alias, so it can never be launched AS one.
MODEL_DEFAULT = "default"
_SEMVERISH_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_PACKAGE_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PATH_SEG_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_LABEL_MAX = 64


class ManifestError(ValueError):
    """A manifest that does not validate. `field` is a dotted path into the document."""

    def __init__(self, field: str, reason: str):
        super().__init__(f"{field}: {reason}" if field else reason)
        self.field = field
        self.reason = reason


# --- the id-pattern grammar ----------------------------------------------------------------------
#
# A manifest's `session_id.pattern` reaches `parse_key`, the single gate every route call passes
# through, so a sloppy pattern weakens every route. Rather than validate an arbitrary regex after
# the fact, the pattern is written in a grammar too small to be sloppy: anchors, literal characters,
# character classes and counts. No `.`, no `*`, no `?`, no groups, no alternation, no
# backreferences — so there is no `.*`, and nothing matches a path separator or a shell
# metacharacter unless a class literally lists it.
#
# Two further rules make it safe to MATCH, not only to read (Hermes on PR #1112):
# * a range stays inside one character category (`0-9`, `a-z`, `A-Z`) and runs low to high —
#   `[z-a]` does not compile and `[0-z]` silently admits `;`, `[`, `^` …;
# * no atom may match the empty string (`{0,m}` is refused), and a variable-length repetition
#   (`+`, or `{n,m}` with n < m) may not share a single character with the atom that follows it.
#   Then every atom consumes at least one character, the end of every run is decided by the first
#   character that is not in it, the engine never has two ways to split the input, and matching
#   stays linear — `^[a]+[a]+…b$` and `^([a]+[b]{0,1})…c$`, which backtrack for seconds on 128
#   characters, are refused here instead.
_LIT_RE = re.compile(r"[A-Za-z0-9_-]")
_QUANT_RE = re.compile(r"\{(\d{1,3})(?:,(\d{1,3}))?\}|\+")
_CLS_ITEM_RE = re.compile(r"([A-Za-z0-9])-([A-Za-z0-9])|([A-Za-z0-9_])|\\-")
_PATTERN_MAX = 200
_CATEGORIES = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _class_set(body: str, where: str) -> frozenset[str]:
    chars: set[str] = set()
    i = 0
    while i < len(body):
        m = _CLS_ITEM_RE.match(body, i)
        if not m:
            raise ManifestError(where, f"unsupported character-class syntax near {body[i:]!r}")
        lo, hi, single = m.group(1), m.group(2), m.group(3)
        if lo is not None:
            cat = next((c for c in _CATEGORIES if lo in c), "")
            if hi not in cat or cat.index(hi) < cat.index(lo):
                raise ManifestError(
                    where, f"range {lo}-{hi} must run low to high within 0-9, a-z or A-Z"
                )
            chars.update(cat[cat.index(lo) : cat.index(hi) + 1])
        else:
            chars.add(single if single is not None else "-")
        i = m.end()
    if not chars:
        raise ManifestError(where, "empty character class")
    return frozenset(chars)


def compile_id_pattern(pattern: Any, where: str = "session_id.pattern") -> re.Pattern:
    if not isinstance(pattern, str) or not pattern:
        raise ManifestError(where, "must be a non-empty string")
    if len(pattern) > _PATTERN_MAX:
        raise ManifestError(where, f"longer than {_PATTERN_MAX} characters")
    if not (pattern.startswith("^") and pattern.endswith("$")) or len(pattern) < 3:
        raise ManifestError(where, "must be anchored with ^ and $")
    body = pattern[1:-1]
    atoms: list[tuple[frozenset[str], bool]] = []  # (characters, variable-length?)
    i = 0
    max_len = 0
    while i < len(body):
        if body[i] == "[":
            end = body.find("]", i + 1)
            if end < 0:
                raise ManifestError(where, "unterminated character class")
            chars = _class_set(body[i + 1 : end], where)
            is_class = True
            i = end + 1
        elif _LIT_RE.match(body, i):
            chars, is_class = frozenset(body[i]), False
            i += 1
        else:
            raise ManifestError(
                where,
                f"unsupported syntax at offset {i + 1}: only literals [A-Za-z0-9_-], character "
                "classes and {n} / {n,m} / + counts are allowed",
            )
        variable = False
        q = _QUANT_RE.match(body, i)
        if q:
            i = q.end()
            if q.group(0) == "+":
                if not is_class:
                    raise ManifestError(where, "+ may only follow a character class")
                variable = True
                max_len += MAX_NATIVE_ID_LEN  # unbounded; the id length cap bounds it
            else:
                lo = int(q.group(1))
                hi = int(q.group(2)) if q.group(2) is not None else lo
                if lo == 0:
                    # A nullable atom lets the atoms on either side of it touch, which is how
                    # `[a]+[b]{0,1}[a]+…` backtracked past the adjacency rule (Hermes on #1112).
                    raise ManifestError(where, "every atom must match at least one character")
                if hi < lo:
                    raise ManifestError(where, "count {n,m} needs n <= m")
                variable = hi > lo
                max_len += hi
        else:
            max_len += 1
        if atoms and atoms[-1][1] and atoms[-1][0] & chars:
            raise ManifestError(
                where,
                "a variable-length repetition may not overlap the atom after it "
                "(ambiguous patterns backtrack catastrophically)",
            )
        atoms.append((chars, variable))
    if max_len == 0:
        raise ManifestError(where, "matches only the empty string")
    try:
        # `\Z`, not `$`: `$` also matches just before a trailing newline, and this compiled
        # pattern is read by callers that use `.match` (independent review of PR #1112).
        return re.compile(pattern[:-1] + r"\Z")
    except re.error as e:  # pragma: no cover — the grammar above admits nothing re rejects
        raise ManifestError(where, f"does not compile: {e}") from None


# --- the document -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    id: str
    label: str
    publisher: str
    version: str
    kind: str


@dataclass(frozen=True)
class Binary:
    name: str
    aliases: tuple[str, ...]
    env_var: str | None
    search_paths: tuple[str, ...]
    version_flag: str | None
    #: Also look in npm's global bin dir (`npm prefix -g`/bin) — `doctor`'s discovery only, for CLIs
    #: whose vendor installs them with `npm i -g` (codex, gemini). Never consulted at launch: the
    #: launcher execs what `doctor` wrote to the env file, or a `search_paths` hit (§2b).
    search_npm_global: bool = False


@dataclass(frozen=True)
class SessionId:
    pattern: re.Pattern
    mint: str
    legacy_bare_id: bool

    def accepts(self, native: str) -> bool:
        return (
            isinstance(native, str)
            and 0 < len(native) <= MAX_NATIVE_ID_LEN
            # An id is placed in argv after a flag or a subcommand; one starting with "-" would be
            # read as an option by the CLI, whatever the pattern admits.
            and not native.startswith("-")
            and self.pattern.fullmatch(native) is not None
        )


@dataclass(frozen=True)
class Store:
    root: str
    env_override: str | None
    layout: str
    read_only: bool
    paths: Mapping[str, str]
    #: Per-path env overrides (`store.path_env`): a named path an operator or a test can point
    #: elsewhere without moving the whole root (opencode's `AGENT_SESSIONS_OPENCODE_DB`/`_LOG`).
    path_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ArgvStep:
    kind: str
    flag: str | None = None
    subcommand: str | None = None


@dataclass(frozen=True)
class ModelLaunch:
    """`launch.model` (#1189): how the engine takes a model at launch. The VALUE is never here."""

    kind: str
    flag: str
    #: Whether the engine honours the flag on a RESUME. False: a resume asking for a model other
    #: than the one the session recorded is refused before spawn; a new session must be started.
    on_resume: bool = False


@dataclass(frozen=True)
class Launch:
    resume: ArgvStep
    new: ArgvStep | None
    base_args: tuple[str, ...]
    bypass: tuple[str, ...]
    bypass_on: str
    admission: str
    model: ModelLaunch | None = None


@dataclass(frozen=True)
class Usage:
    source: str
    kind: str | None
    probe_token: str | None
    #: `usage.access` (#1167): the check that observes whether the vendor refuses this account.
    access: str | None = None


@dataclass(frozen=True)
class Terminal:
    repaint: str
    ready: str
    menu: str
    menu_digit_submits: bool
    #: `terminal.permission` (#1213): the tool-permission dialog kind, or "none".
    permission: str = "none"


@dataclass(frozen=True)
class Model:
    id: str
    context_window: int | None
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Display:
    name: str
    badge: str
    accent: str
    id_prefix: str | None
    #: Roster position — scan and display order. Lower first; ties break on id.
    order: int = 500


@dataclass(frozen=True)
class Install:
    kind: str
    authority: str
    package: str
    version: str
    digest: str
    entrypoint: str


@dataclass(frozen=True)
class Endpoint:
    kind: str


@dataclass(frozen=True)
class Manifest:
    contract: int
    identity: Identity
    #: `runtime.kind` (#853 §7). Every consumer that needs a terminal asks this first.
    runtime: str
    #: None exactly for a non-`pty` runtime — nothing is executed, so there is no binary to name.
    binary: Binary | None
    session_id: SessionId
    store: Store | None
    #: None exactly for a non-`pty` runtime — nothing is launched.
    launch: Launch | None
    capabilities: Mapping[str, bool]
    transcript_kind: str
    transcript_strict: bool
    usage: Usage
    terminal: Terminal
    start_evidence: str
    maintenance: tuple[str, ...]
    models: tuple[Model, ...]
    models_configured_elsewhere: bool
    display: Display
    install: Install | None
    signin_kind: str
    signin_subcommand: str | None
    verify: tuple[str, ...]
    source: str = field(default="", compare=False)
    #: sha256 of the manifest as loaded. An install record or an operator confirmation is bound to
    #: it, so editing the manifest (a new entrypoint name, new search paths) voids both.
    digest: str = field(default="", compare=False)
    #: `[endpoint]` (#1209): present exactly for a `chat` runtime. The wire format only.
    endpoint: Endpoint | None = None
    #: `instructions.files` (#1189): workspace-root instruction files the engine reads, bare names
    #: from `kinds.INSTRUCTION_FILES`. Empty = declares none.
    instructions: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.identity.id

    def can(self, capability: str) -> bool:
        """Default-deny: an undeclared capability is off."""
        return bool(self.capabilities.get(capability, False))


# --- helpers ------------------------------------------------------------------------------------


class _Reader:
    """Reads one table, refusing unknown keys and recording what was consumed."""

    def __init__(self, table: Any, where: str):
        if not isinstance(table, Mapping):
            raise ManifestError(where, "must be a table")
        self.t = table
        self.where = where
        self.seen: set[str] = set()

    def _f(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def has(self, key: str) -> bool:
        return key in self.t

    def raw(self, key: str, default: Any = ...) -> Any:
        self.seen.add(key)
        if key not in self.t:
            if default is ...:
                raise ManifestError(self._f(key), "is required")
            return default
        return self.t[key]

    def str(
        self,
        key: str,
        default: Any = ...,
        *,
        pattern: re.Pattern | None = None,
        one_of: frozenset[str] | None = None,
        max_len: int = _LABEL_MAX,
    ) -> Any:
        v = self.raw(key, default)
        if v is default and default is not ...:
            return v
        if not isinstance(v, str) or not v or len(v) > max_len:
            raise ManifestError(
                self._f(key), f"must be a non-empty string of at most {max_len} characters"
            )
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ManifestError(self._f(key), "must not contain control characters")
        if pattern is not None and not pattern.fullmatch(v):
            raise ManifestError(self._f(key), f"has an invalid shape: {v!r}")
        if one_of is not None and v not in one_of:
            raise ManifestError(self._f(key), f"must be one of {sorted(one_of)}, not {v!r}")
        return v

    def bool(self, key: str, default: bool = False) -> bool:
        v = self.raw(key, default)
        if not isinstance(v, bool):
            raise ManifestError(self._f(key), "must be true or false")
        return v

    def int(self, key: str, default: Any = ..., *, lo: int = 0, hi: int = 1 << 31) -> Any:
        v = self.raw(key, default)
        if v is default and default is not ...:
            return v
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ManifestError(self._f(key), f"must be an integer in [{lo}, {hi}]")
        return v

    def strs(
        self,
        key: str,
        *,
        one_of: frozenset[str] | None = None,
        pattern: re.Pattern | None = None,
        max_items: int = 16,
    ) -> tuple[str, ...]:
        v = self.raw(key, [])
        if not isinstance(v, list) or len(v) > max_items:
            raise ManifestError(self._f(key), f"must be a list of at most {max_items} strings")
        out: list[str] = []
        for i, item in enumerate(v):
            f = f"{self._f(key)}[{i}]"
            if not isinstance(item, str) or not item:
                raise ManifestError(f, "must be a non-empty string")
            if one_of is not None and item not in one_of:
                raise ManifestError(f, f"must be one of {sorted(one_of)}, not {item!r}")
            if pattern is not None and not pattern.fullmatch(item):
                raise ManifestError(f, f"has an invalid shape: {item!r}")
            if item in out:
                raise ManifestError(f, f"duplicate {item!r}")
            out.append(item)
        return tuple(out)

    def table(self, key: str, required: bool = True) -> _Reader | None:
        if key not in self.t:
            self.seen.add(key)
            if required:
                raise ManifestError(self._f(key), "is required")
            return None
        self.seen.add(key)
        return _Reader(self.t[key], self._f(key))

    def done(self) -> None:
        extra = sorted(str(k) for k in self.t if k not in self.seen)
        if extra:
            raise ManifestError(
                self._f(extra[0]), "unknown field (manifests reject unknown fields)"
            )


def anchored_path(value: Any, where: str) -> str:
    """An absolute path or a `~/`-relative one, with no glob, no `..`, no `.`, no empty segment.

    Returned unexpanded — expansion happens against the runtime home, so a manifest cannot pin a
    path to someone else's home by naming it.
    """
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ManifestError(where, "must be a non-empty path string")
    if value.startswith("~/"):
        segs = value[2:].split("/")
    elif value.startswith("/"):
        segs = value[1:].split("/")
    else:
        raise ManifestError(where, "must be absolute or start with ~/")
    for s in segs:
        if s in ("", ".", "..") or not _PATH_SEG_RE.fullmatch(s):
            raise ManifestError(
                where, f"segment {s!r} is not allowed (no globs, no '..', no empty segments)"
            )
    return value


def relative_path(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or len(value) > 256:
        raise ManifestError(where, "must be a relative path inside the declared root")
    for s in value.split("/"):
        if s in ("", ".", "..") or not _PATH_SEG_RE.fullmatch(s):
            raise ManifestError(
                where, f"segment {s!r} is not allowed (no globs, no '..', no empty segments)"
            )
    return value


# --- contract migrations -------------------------------------------------------------------------

#: `MIGRATIONS[n]` rewrites a contract-n document into contract-(n+1) shape. Empty while contract 1
#: is the only contract; the chain exists so the first real migration is an entry, not a redesign.
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


def _migrate(doc: dict) -> dict:
    c = doc.get("contract")
    if isinstance(c, bool) or not isinstance(c, int):
        raise ManifestError("contract", "is required and must be an integer")
    if c > kinds.CONTRACT_CURRENT:
        raise ManifestError(
            "contract",
            f"{c} needs a newer BattleLab (this build reads up to {kinds.CONTRACT_CURRENT})",
        )
    if c < 1:
        raise ManifestError("contract", f"{c} is not a valid contract")
    while c < kinds.CONTRACT_CURRENT:
        step = MIGRATIONS.get(c)
        if step is None:
            raise ManifestError("contract", f"no migration from contract {c}")
        doc = step(dict(doc))
        c += 1
        doc["contract"] = c
    return doc


# --- parse ---------------------------------------------------------------------------------------


def parse(doc: Any, *, source: str = "", digest: str | None = None) -> Manifest:
    if not isinstance(doc, Mapping):
        raise ManifestError("", "a manifest must be a table")
    if digest is None:
        digest = hashlib.sha256(
            json.dumps(doc, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    doc = _migrate(dict(doc))
    top = _Reader(doc, "")
    contract = top.int("contract", lo=1, hi=kinds.CONTRACT_CURRENT)

    r = top.table("identity")
    identity = Identity(
        id=r.str("id", pattern=_ID_RE, max_len=24),
        label=r.str("label"),
        publisher=r.str("publisher"),
        version=r.str("version", pattern=_SEMVERISH_RE),
        kind=r.str("kind", "agent", one_of=kinds.IDENTITY_KINDS),
    )
    if identity.id in kinds.ROUTE_RESERVED_IDS:
        raise ManifestError("identity.id", "is reserved for an Agents settings route")
    r.done()

    # `runtime` (#853 §7) is read FIRST: it decides which blocks are required and which are
    # forbidden. Absent means `pty`, which is every engine that predates #1209, so contract 1 needs
    # no migration. The set holds only what this build can run; anything else is refused rather
    # than half-run.
    r = top.table("runtime", required=False)
    runtime = "pty"
    if r is not None:
        raw_rt = r.raw("kind", "pty")
        if raw_rt not in kinds.RUNTIME_KINDS:
            raise ManifestError(
                "runtime.kind",
                f"{raw_rt!r} needs a newer BattleLab "
                f"(this build runs {sorted(kinds.RUNTIME_KINDS)})",
            )
        runtime = raw_rt
        r.done()

    binary: Binary | None = None
    endpoint: Endpoint | None = None
    if runtime == "chat":
        # Nothing is executed, launched or typed into: a block that describes a process is a
        # contradiction, refused with its name rather than ignored.
        for block in kinds.PTY_ONLY_BLOCKS:
            if top.has(block):
                raise ManifestError(block, "is forbidden for runtime 'chat' (nothing is executed)")
        r = top.table("endpoint")
        endpoint = Endpoint(kind=r.str("kind", one_of=kinds.ENDPOINT_KINDS))
        r.done()
    else:
        if top.has("endpoint"):
            raise ManifestError("endpoint", f"is only allowed for runtime 'chat', not {runtime!r}")
        r = top.table("binary")
        name = r.str("name", pattern=_BIN_NAME_RE)
        aliases = r.strs("aliases", pattern=_BIN_NAME_RE, max_items=4)
        env_var = r.str("env_var", None, pattern=_ENV_BIN_RE)
        search = tuple(
            anchored_path(p, f"binary.search_paths[{i}]")
            for i, p in enumerate(r.strs("search_paths", max_items=8))
        )
        version_flag = r.str("version_flag", None, one_of=kinds.VERSION_FLAGS)
        npm_global = r.bool("search_npm_global")
        r.done()
        if name != identity.id and name not in aliases:
            raise ManifestError("binary.name", "must be the plugin id or one of binary.aliases")
        binary = Binary(name, aliases, env_var, search, version_flag, npm_global)

    r = top.table("session_id")
    sid = SessionId(
        pattern=compile_id_pattern(r.raw("pattern")),
        mint=r.str("mint", one_of=kinds.MINT_KINDS),
        legacy_bare_id=r.bool("legacy_bare_id"),
    )
    r.done()

    store: Store | None = None
    r = top.table("store", required=False)
    if r is not None:
        root = anchored_path(r.raw("root"), "store.root")
        env_override = r.str("env_override", None, pattern=_ENV_DIR_RE)
        layout = r.str("layout", one_of=kinds.STORE_LAYOUTS)
        read_only = r.bool("read_only", True)
        paths: dict[str, str] = {}
        pr = r.table("paths", required=False)
        if pr is not None:
            for k in list(pr.t):
                if k not in kinds.STORE_PATH_NAMES:
                    raise ManifestError(
                        f"store.paths.{k}", f"must be one of {sorted(kinds.STORE_PATH_NAMES)}"
                    )
                paths[k] = relative_path(pr.raw(k), f"store.paths.{k}")
            pr.done()
        path_env: dict[str, str] = {}
        er = r.table("path_env", required=False)
        if er is not None:
            for k in list(er.t):
                if k not in paths:
                    raise ManifestError(
                        f"store.path_env.{k}", "must name a declared store.paths entry"
                    )
                path_env[k] = er.str(k, pattern=_ENV_DIR_RE)
            er.done()
        r.done()
        store = Store(root, env_override, layout, read_only, paths, path_env)

    launch: Launch | None = None
    if runtime == "pty":
        r = top.table("launch")
        launch = _launch(r)
        r.done()

    r = top.table("capabilities", required=False)
    caps: dict[str, bool] = {}
    if r is not None:
        for c in kinds.CAPABILITIES:
            caps[c] = r.bool(c)
        r.done()

    r = top.table("transcript", required=False)
    t_kind, t_strict = "none", False
    if r is not None:
        t_kind = r.str("kind", one_of=kinds.TRANSCRIPT_KINDS)
        t_strict = r.bool("strict")
        r.done()

    r = top.table("usage", required=False)
    usage = Usage("none", None, None)
    if r is not None:
        src = r.str("source", one_of=kinds.USAGE_SOURCES)
        ukind = r.str("kind", None, one_of=kinds.USAGE_KINDS)
        token = r.str("probe_token", None, one_of=kinds.USAGE_PROBE_TOKENS)
        access = r.str("access", None, one_of=kinds.USAGE_ACCESS_KINDS)
        r.done()
        if src in ("plan", "tokens") and ukind is None:
            raise ManifestError("usage.kind", f"is required for source {src!r}")
        if src in ("manual", "none") and ukind is not None:
            raise ManifestError("usage.kind", f"must be absent for source {src!r}")
        if ukind is not None and kinds.is_cli_probe(ukind) != (token is not None):
            raise ManifestError(
                "usage.probe_token", "is required for a cli-probe kind and forbidden otherwise"
            )
        usage = Usage(src, ukind, token, access)

    r = top.table("terminal", required=False)
    terminal = Terminal("none", "bytes", "none", False)
    if r is not None:
        terminal = Terminal(
            repaint=r.str("repaint", "none", one_of=kinds.REPAINT_KINDS),
            ready=r.str("ready", "bytes", one_of=kinds.READY_KINDS),
            menu=r.str("menu", "none", one_of=kinds.MENU_KINDS),
            menu_digit_submits=r.bool("menu_digit_submits"),
            permission=r.str("permission", "none", one_of=kinds.PERMISSION_KINDS),
        )
        r.done()
        if terminal.menu_digit_submits and terminal.menu == "none":
            raise ManifestError("terminal.menu_digit_submits", "needs a terminal.menu parser")

    r = top.table("unattended", required=False)
    start_evidence = "none"
    if r is not None:
        start_evidence = r.str("start_evidence", "none", one_of=kinds.START_EVIDENCE_KINDS)
        r.done()

    maintenance = top.strs("maintenance", one_of=kinds.MAINTENANCE_KINDS, max_items=4)

    models: list[Model] = []
    r = top.table("models", required=False)
    configured_elsewhere = False
    if r is not None:
        configured_elsewhere = r.bool("configured_elsewhere")
        items = r.raw("list", [])
        if not isinstance(items, list) or len(items) > 64:
            raise ManifestError("models.list", "must be a list of at most 64 tables")
        for i, item in enumerate(items):
            mr = _Reader(item, f"models.list[{i}]")
            models.append(
                Model(
                    id=mr.str("id", pattern=_MODEL_ID_RE, max_len=96),
                    context_window=mr.int("context_window", None, lo=1, hi=100_000_000),
                    aliases=mr.strs("aliases", pattern=_MODEL_ID_RE, max_items=8),
                )
            )
            mr.done()
        r.done()
    if len({m.id for m in models}) != len(models):
        raise ManifestError("models.list", "duplicate model id")
    # Aliases are accepted on input and canonicalised to their model's id before anything is stored
    # or launched (#1189), so every name must resolve to exactly one model: an alias that is also
    # another model's id or alias is ambiguous, and `default` is the picker's no-flag sentinel.
    names: set[str] = set()
    for i, mdl in enumerate(models):
        for name in (mdl.id, *mdl.aliases):
            if name.lower() == MODEL_DEFAULT:
                raise ManifestError(f"models.list[{i}]", f"{MODEL_DEFAULT!r} is reserved")
            if name in names:
                raise ManifestError(
                    f"models.list[{i}]", f"{name!r} names more than one model (ambiguous alias)"
                )
            names.add(name)

    instructions = _instructions(top)

    r = top.table("display")
    display = Display(
        name=r.str("name"),
        badge=r.str("badge", pattern=_BADGE_RE, max_len=3),
        accent=r.str("accent", one_of=kinds.ACCENT_TOKENS),
        id_prefix=r.str("id_prefix", None, pattern=_PREFIX_RE, max_len=17),
        order=r.int("order", 500, lo=0, hi=999),
    )
    r.done()

    install = None
    r = top.table("install", required=False)
    if r is not None:
        ikind = r.str("kind", one_of=kinds.INSTALL_KINDS)
        install = Install(
            kind=ikind,
            authority=r.str("authority", one_of=kinds.INSTALL_AUTHORITIES[ikind], max_len=253),
            package=r.str("package", pattern=_PACKAGE_RE, max_len=200),
            version=r.str("version", pattern=_SEMVERISH_RE),
            digest=r.str("digest", pattern=_DIGEST_RE, max_len=71),
            entrypoint=relative_path(r.raw("entrypoint"), "install.entrypoint"),
        )
        r.done()
        assert binary is not None  # `install` is a PTY_ONLY_BLOCK, refused above for chat
        if install.entrypoint.rsplit("/", 1)[-1] not in (binary.name, *binary.aliases):
            raise ManifestError(
                "install.entrypoint", "must end in binary.name or one of binary.aliases"
            )

    r = top.table("signin", required=False)
    signin_kind, signin_sub = "none", None
    if r is not None:
        signin_kind = r.str("kind", one_of=frozenset(kinds.SIGNIN_KINDS))
        allowed = kinds.SIGNIN_KINDS[signin_kind]
        if allowed:
            signin_sub = r.str("subcommand", one_of=allowed)
        elif r.has("subcommand"):
            raise ManifestError("signin.subcommand", f"is forbidden for kind {signin_kind!r}")
        r.done()

    verify = top.strs("verify", one_of=kinds.VERIFY_CHECKS, max_items=len(kinds.VERIFY_CHECKS))
    top.done()

    m = Manifest(
        contract=contract,
        identity=identity,
        runtime=runtime,
        binary=binary,
        session_id=sid,
        store=store,
        launch=launch,
        capabilities=caps,
        transcript_kind=t_kind,
        transcript_strict=t_strict,
        usage=usage,
        terminal=terminal,
        start_evidence=start_evidence,
        maintenance=maintenance,
        models=tuple(models),
        models_configured_elsewhere=configured_elsewhere,
        display=display,
        install=install,
        signin_kind=signin_kind,
        signin_subcommand=signin_sub,
        verify=verify,
        source=source,
        digest=digest,
        endpoint=endpoint,
        instructions=instructions,
    )
    _cross_check(m)
    return m


def _step(r: _Reader, allowed: frozenset[str]) -> ArgvStep:
    kind = r.str("kind", one_of=allowed)
    flag = sub = None
    if kind == "flag":
        flag = r.str("flag", one_of=kinds.RESUME_FLAGS)
    elif kind == "subcommand":
        sub = r.str("subcommand", one_of=kinds.RESUME_SUBCOMMANDS)
    elif kind == "positional-dir" and allowed is kinds.RESUME_KINDS:
        flag = r.str("flag", one_of=kinds.RESUME_FLAGS)
    elif kind == "pin-flag":
        flag = r.str("flag", one_of=kinds.NEW_PIN_FLAGS)
    elif kind == "cwd-flag":
        flag = r.str("flag", one_of=kinds.CWD_FLAGS)
    r.done()
    return ArgvStep(kind, flag, sub)


def _launch(r: _Reader) -> Launch:
    rr = r.table("resume")
    resume = _step(rr, kinds.RESUME_KINDS)
    nr = r.table("new", required=False)
    new = _step(nr, kinds.NEW_KINDS) if nr is not None else None
    base_args = r.strs("base_args", one_of=kinds.BASE_ARGS, max_items=2)
    bypass = r.strs("bypass", one_of=kinds.BYPASS_FLAGS, max_items=3)
    bypass_on = r.str("bypass_on", "both", one_of=kinds.BYPASS_ON)
    admission = r.str("admission", "none", one_of=kinds.ADMISSION_KINDS)
    model = _launch_model(r)
    return Launch(resume, new, base_args, bypass, bypass_on, admission, model)


def _launch_model(r: _Reader) -> ModelLaunch | None:
    """`launch.model` (#1189): a kind and a flag from closed sets, and `on_resume`. The table is
    closed like every other: an unknown key inside it is refused."""
    mr = r.table("model", required=False)
    if mr is None:
        return None
    kind = mr.str("kind", one_of=kinds.MODEL_KINDS)
    flag = mr.str("flag", one_of=kinds.MODEL_FLAGS)
    on_resume = mr.bool("on_resume")
    mr.done()
    return ModelLaunch(kind, flag, on_resume)


def _instructions(top: _Reader) -> tuple[str, ...]:
    """`instructions.files` (#1189): bare workspace-root names from a closed set. The vocabulary
    already excludes separators; the explicit check keeps that true if a member is ever added."""
    r = top.table("instructions", required=False)
    if r is None:
        return ()
    files = r.strs("files", one_of=kinds.INSTRUCTION_FILES, max_items=len(kinds.INSTRUCTION_FILES))
    r.done()
    for i, f in enumerate(files):
        if "/" in f or "\\" in f or f in (".", ".."):
            raise ManifestError(f"instructions.files[{i}]", "must be a bare file name")
    return files


def _cross_check(m: Manifest) -> None:
    caps = m.capabilities
    if m.identity.kind == "terminal":
        for c in sorted(kinds.AGENT_ONLY_CAPABILITIES):
            if caps.get(c):
                raise ManifestError(
                    f"capabilities.{c}",
                    "a terminal plugin has no agent behind it; text typed into it runs as a "
                    "command",
                )
    if m.runtime == "chat":
        for c in sorted(kinds.PTY_ONLY_CAPABILITIES):
            if caps.get(c):
                raise ManifestError(
                    f"capabilities.{c}",
                    "presumes a terminal or a process; a 'chat' plugin has neither",
                )
        if m.store is None:
            raise ManifestError("store", "is required for runtime 'chat'")
        if m.store.layout != "battlelab-chat":
            raise ManifestError("store.layout", "must be 'battlelab-chat' for runtime 'chat'")
        if m.transcript_kind != "battlelab-chat":
            raise ManifestError("transcript.kind", "must be 'battlelab-chat' for runtime 'chat'")
        if m.store.read_only:
            raise ManifestError("store.read_only", "must be false: BattleLab writes this store")
        if m.models:
            # A chat agent's model is the OPERATOR's endpoint configuration (#1209), never the
            # manifest's: a list here would offer choices no launch could honour (#1189).
            raise ManifestError(
                "models.list", "must be empty for runtime 'chat' (the endpoint config picks it)"
            )
    else:
        if m.store is not None and m.store.layout == "battlelab-chat":
            raise ManifestError("store.layout", "'battlelab-chat' is only for runtime 'chat'")
        if m.transcript_kind == "battlelab-chat":
            raise ManifestError("transcript.kind", "'battlelab-chat' is only for runtime 'chat'")
        if m.usage.kind == "chat-response-tokens":
            raise ManifestError("usage.kind", "'chat-response-tokens' is only for runtime 'chat'")
        _cross_check_launch(m)
    if caps.get("owns_transcript") and m.transcript_kind == "none":
        raise ManifestError("capabilities.owns_transcript", "needs a transcript kind")
    if m.transcript_strict and m.transcript_kind == "none":
        raise ManifestError("transcript.strict", "needs a transcript kind")
    if "sqlite-vacuum" in m.maintenance and (m.store is None or "db" not in m.store.paths):
        raise ManifestError("maintenance", "sqlite-vacuum needs store.paths.db")
    if m.start_evidence == "opencode-log" and (m.store is None or "log" not in m.store.paths):
        raise ManifestError("unattended.start_evidence", "opencode-log needs store.paths.log")
    if m.usage.source == "none" and "usage" in m.verify:
        raise ManifestError("verify", "cannot verify usage for a plugin that declares none")
    if m.install is not None and m.binary is not None and m.binary.env_var is None:
        # A managed plugin must say which env override would flip it to adopted (§2b), so the flip
        # can be detected rather than silently honoured.
        raise ManifestError("binary.env_var", "is required when install is declared")


def _cross_check_launch(m: Manifest) -> None:
    """The `launch` rules, which exist only for a `pty` runtime."""
    caps = m.capabilities
    assert m.launch is not None
    if caps.get("new") and m.launch.new is None:
        raise ManifestError("launch.new", "is required when capabilities.new is true")
    if not caps.get("new") and m.launch.new is not None:
        raise ManifestError("launch.new", "declared but capabilities.new is false")
    if (
        m.launch.new is not None
        and m.launch.new.kind == "pin-flag"
        and m.session_id.mint != "pinned"
    ):
        raise ManifestError("launch.new", "pin-flag needs session_id.mint = 'pinned'")
    if caps.get("resume") and m.launch.resume.kind == "fresh" and m.identity.kind == "agent":
        raise ManifestError(
            "launch.resume", "an agent that can resume needs a resume kind other than 'fresh'"
        )
    if m.launch.admission != "none" and (m.store is None or "db" not in m.store.paths):
        raise ManifestError("launch.admission", "needs store.paths.db")
    if m.launch.model is not None:
        # A model flag with nothing to choose from, or on an engine whose model is its own config,
        # would be a picker that can only ever offer `default` — or one that lies (#1189).
        if m.models_configured_elsewhere:
            raise ManifestError(
                "launch.model", "an engine whose model is configured elsewhere takes no model flag"
            )
        if not m.models:
            raise ManifestError("launch.model", "needs a non-empty models.list")
        if m.identity.kind == "terminal":
            raise ManifestError("launch.model", "a terminal plugin has no model")


# --- files ---------------------------------------------------------------------------------------

MANIFEST_NAMES = ("plugin.toml", "plugin.json")


def load_bytes(data: bytes, *, name: str, source: str = "") -> Manifest:
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("", f"larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError("", "not UTF-8") from None
    try:
        if name.endswith(".toml"):
            doc = tomllib.loads(text)
        elif name.endswith(".json"):
            doc = json.loads(text)
        else:
            raise ManifestError("", f"unknown manifest format {name!r}")
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as e:
        raise ManifestError("", f"does not parse: {e}") from None
    except RecursionError:
        # Well under the byte cap, a few thousand nested brackets still exhaust the parser's
        # stack; that is a malformed manifest, not a reason to take the loader down.
        raise ManifestError("", "is nested too deeply") from None
    try:
        return parse(doc, source=source, digest=hashlib.sha256(data).hexdigest())
    except RecursionError:
        raise ManifestError("", "is nested too deeply") from None


def load_fd(fd: int, *, name: str, source: str = "") -> Manifest:
    """Parse the manifest on an ALREADY-VERIFIED descriptor — the loader's path for local
    manifests, so the bytes parsed are the bytes whose file was checked (Hermes on PR #1112)."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise ManifestError("", "is not a regular file")
    if st.st_size > MAX_MANIFEST_BYTES:
        raise ManifestError("", f"larger than {MAX_MANIFEST_BYTES} bytes")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, total = [], 0
    while total <= MAX_MANIFEST_BYTES:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return load_bytes(b"".join(chunks), name=name, source=source)


def load_file(path: Path, *, source: str = "") -> Manifest:
    """Parse an in-tree manifest. Local manifests go through `open_verified` + `load_fd`."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        return load_fd(fd, name=path.name, source=source or str(path))
    finally:
        os.close(fd)
