"""Third-party MCP servers in the Library (#2163): registered once, checked, delivered per project.

The operator registers a server once — a remote (streamable HTTP) URL, or an installed package
(P1b) — with its plain settings and the NAMES of the variables that hold its credentials. Every
new agent launch in a project the server is enabled for gets it through the per-launch tool
channel (`agent_tools`), as ``python -I -m agent_sessions.mcp_launch <id>``: only an id and the
secrets capability's PATHS ride argv. The launcher asks the secrets broker (op ``mcp``) for the
resolved settings, which this module computes. See ``docs/invariants/mcp-servers.md``.

**Credentials resolve per project with the variables store's own rule**: the project's own
record of that name, else the library record it binds by ``ref: global``, else missing — never
another project's value, never an unbound library value. The ``shared`` scope (a check of the
library values themselves) reads the library record directly.

**Staleness is derived, never stored.** A check result counts only while its ``(revision,
digest)`` equals the digest computed NOW from a strict read of both stores (`resolve`). Every
decision — settlement, enable, delivery — takes the broker's ``access_fence`` first, then this
store's lock (`fence`), and computes the digest inside them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .atomicjson import atomic_write_json, json_write_lock

log = logging.getLogger(__name__)

STORE_VERSION = 1
SERVERS_MAX = 64
SETTINGS_MAX = 32
ARGS_MAX = 32
CHECKS_PER_SERVER = 20
VALUE_MAX = 2000
URL_MAX = 2048
TOOLS_MAX = 512

SHARED = "shared"
TRANSPORTS = ("stdio", "http")
SOURCE_KINDS = ("remote", "npm", "pypi", "go")
DELIVER = ("env", "header")
SETTING_KINDS = ("plain", "credential")
OUTCOMES = ("passed", "failed", "superseded")

ID_RE = re.compile(r"^[0-9a-f]{16}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
RESERVED_PREFIX = "battlelab_"
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
HEADER_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
VARIABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

#: Env names a server may never set: they steer the interpreter, the loader or BattleLab itself.
ENV_FORBIDDEN = frozenset({"PATH", "HOME", "LANG", "SHELL", "USER", "LOGNAME", "NODE_OPTIONS"})
ENV_FORBIDDEN_PREFIXES = ("LD_", "PYTHON", "BATTLELAB_", "AGENT_SESSIONS_", "DYLD_", "UV_")
#: Headers the relay owns.
HEADER_FORBIDDEN = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "accept",
        "connection",
        "transfer-encoding",
        "mcp-session-id",
        "mcp-protocol-version",
        "last-event-id",
    }
)

#: Test hook: admit ``http://`` to a loopback host (the fixture HTTP server). Never set in the app.
_ALLOW_LOOPBACK_HTTP = False


class RegistryError(ValueError):
    """A request the registry refuses; the message is safe to show (it never echoes a value)."""


class NotFound(RegistryError):
    pass


class Conflict(RegistryError):
    """The record changed since the caller read it (``expected`` revision mismatch)."""


def store_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_MCP_SERVERS",
            str(Path.home() / ".config" / "agent-sessions" / "mcp-servers.json"),
        )
    )


# ---- validation --------------------------------------------------------------------------------


def _text(raw: object, what: str, *, cap: int = VALUE_MAX, line: bool = True) -> str:
    if not isinstance(raw, str):
        raise RegistryError(f"{what} is text")
    if len(raw) > cap:
        raise RegistryError(f"{what} is at most {cap} characters")
    if "\0" in raw or (line and ("\n" in raw or "\r" in raw)):
        raise RegistryError(f"{what} is one line of text")
    return raw


def validate_url(raw: object) -> str:
    url = _text(raw, "the server URL", cap=URL_MAX).strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        raise RegistryError("the server URL is not a URL") from None
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not (
        _ALLOW_LOOPBACK_HTTP and parts.scheme == "http" and loopback
    ):
        raise RegistryError("the server URL must be https")
    if not parts.hostname:
        raise RegistryError("the server URL has no host")
    if parts.username is not None or parts.password is not None:
        raise RegistryError("put credentials in a setting, never in the URL")
    if parts.fragment:
        raise RegistryError("the server URL has no #fragment")
    return url


def _source(raw: object, *, resolved: bool = False) -> dict:
    """A server's source. A Go module (#2427) is BUILT from a repository's code, so it is taken
    only from a link's server-held resolution (``resolved``), never typed in."""
    if not isinstance(raw, dict):
        raise RegistryError("source is an object")
    kind = raw.get("kind")
    if kind == "remote":
        unknown = set(raw) - {"kind", "url"}
        if unknown:
            raise RegistryError("unknown source field")
        return {"kind": "remote", "url": validate_url(raw.get("url"))}
    if kind == "go" and not resolved:
        raise RegistryError(
            "a Go module is added only from its repository link (Add MCP server → Repository "
            "link), which proves the code at its commit"
        )
    if kind in ("npm", "pypi", "go"):
        from . import mcp_install

        return mcp_install.validate_source(raw)
    raise RegistryError(f"source kind is one of {', '.join(SOURCE_KINDS)}")


def _transport(source: dict) -> str:
    return "http" if source["kind"] == "remote" else "stdio"


def _setting(raw: object, transport: str) -> dict:
    if not isinstance(raw, dict):
        raise RegistryError("a setting is an object")
    unknown = set(raw) - {"key", "deliver", "kind", "value", "variable", "prefix"}
    if unknown:
        raise RegistryError("unknown setting field")
    deliver = raw.get("deliver") or ("header" if transport == "http" else "env")
    if deliver not in DELIVER:
        raise RegistryError("a setting is delivered as env or header")
    if (deliver == "header") != (transport == "http"):
        raise RegistryError(
            "a remote server takes HTTP headers; a package server takes environment variables"
        )
    key = raw.get("key")
    if deliver == "env":
        if not isinstance(key, str) or not ENV_KEY_RE.fullmatch(key):
            raise RegistryError("an environment variable name is A-Z, 0-9 and _")
        if key in ENV_FORBIDDEN or key.startswith(ENV_FORBIDDEN_PREFIXES):
            raise RegistryError(f"{key} is reserved and cannot be set for a server")
    else:
        if not isinstance(key, str) or not HEADER_KEY_RE.fullmatch(key):
            raise RegistryError("a header name is letters, digits and -")
        if key.lower() in HEADER_FORBIDDEN:
            raise RegistryError(f"the {key} header is set by BattleLab")
    kind = raw.get("kind", "plain")
    if kind not in SETTING_KINDS:
        raise RegistryError("a setting is plain or a credential")
    out: dict = {"key": key, "deliver": deliver, "kind": kind}
    if kind == "plain":
        if "variable" in raw or "prefix" in raw:
            raise RegistryError("a plain setting holds its value")
        out["value"] = _text(raw.get("value", ""), f"the value of {key}")
    else:
        if "value" in raw:
            # A credential's value is never part of a server record: it lives in Variables.
            raise RegistryError("a credential names a variable; its value is stored in Variables")
        var = raw.get("variable")
        if not isinstance(var, str) or not VARIABLE_RE.fullmatch(var):
            raise RegistryError("a credential names a variable (lowercase, digits, _)")
        out["variable"] = var
        prefix = raw.get("prefix", "")
        out["prefix"] = _text(prefix, "the credential prefix", cap=32)
    return out


def _settings(raw: object, transport: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > SETTINGS_MAX:
        raise RegistryError(f"settings is a list of at most {SETTINGS_MAX}")
    out = [_setting(s, transport) for s in raw]
    keys = [s["key"].lower() if s["deliver"] == "header" else s["key"] for s in out]
    if len(set(keys)) != len(keys):
        raise RegistryError("each setting key appears once")
    return out


def _args(raw: object, transport: str) -> list[str]:
    if raw is None:
        return []
    if transport != "stdio" and raw:
        raise RegistryError("a remote server takes no arguments")
    if not isinstance(raw, list) or len(raw) > ARGS_MAX:
        raise RegistryError(f"args is a list of at most {ARGS_MAX}")
    return [_text(a, "an argument", cap=256) for a in raw]


def _probe(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"tool", "arguments", "confirmed_write"}:
        raise RegistryError("probe is {tool, arguments}")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not TOOL_NAME_RE.fullmatch(tool):
        raise RegistryError("probe names a tool")
    arguments = raw.get("arguments") or {}
    if not isinstance(arguments, dict) or len(json.dumps(arguments)) > 2000:
        raise RegistryError("probe arguments are an object of at most 2000 characters")
    return {
        "tool": tool,
        "arguments": arguments,
        "confirmed_write": raw.get("confirmed_write") is True,
    }


def _projects(raw: object) -> str | list[str]:
    if raw == "all":
        return "all"
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 512:
        raise RegistryError('projects is "all" or a list of project ids')
    out = []
    for p in raw:
        if not isinstance(p, str) or not PID_RE.fullmatch(p):
            raise RegistryError("a project id is letters, digits, - and _")
        if p not in out:
            out.append(p)
    return out


def _project_settings(raw: object, settings: list[dict]) -> dict[str, dict[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 512:
        raise RegistryError("project_settings maps a project id to its plain values")
    plain = {s["key"] for s in settings if s["kind"] == "plain"}
    out: dict[str, dict[str, str]] = {}
    for pid, values in raw.items():
        if not isinstance(pid, str) or not PID_RE.fullmatch(pid):
            raise RegistryError("a project id is letters, digits, - and _")
        if not isinstance(values, dict):
            raise RegistryError("a project's settings are an object")
        row = {}
        for key, value in values.items():
            if key not in plain:
                raise RegistryError("a project overrides only the server's plain settings")
            row[key] = _text(value, f"the value of {key}")
        if row:
            out[pid] = row
    return out


def _check_name(name: object, servers: list[dict], own_id: str | None) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise RegistryError("a name is lowercase letters, digits, - and _ (2–32, letter first)")
    if name.startswith(RESERVED_PREFIX):
        raise RegistryError(f"names starting with {RESERVED_PREFIX} are BattleLab's own")
    if any(s["name"] == name and s["id"] != own_id for s in servers):
        raise RegistryError(f"a server named {name} exists")
    return name


# ---- the store ---------------------------------------------------------------------------------


def _empty() -> dict:
    return {"version": STORE_VERSION, "servers": [], "checks": []}


def read() -> dict:
    """The whole document (strict: a damaged or newer store raises `RegistryError`)."""
    path = store_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _empty()
    try:
        doc = json.loads(raw)
    except ValueError:
        raise RegistryError("the MCP server store is damaged") from None
    if not isinstance(doc, dict) or doc.get("version") != STORE_VERSION:
        raise RegistryError("the MCP server store is damaged or from a newer BattleLab")
    if not isinstance(doc.get("servers"), list) or not isinstance(doc.get("checks"), list):
        raise RegistryError("the MCP server store is damaged")
    return doc


@contextlib.contextmanager
def fence() -> Iterator[None]:
    """This store's lock. Callers that also need the broker's ``access_fence`` take it FIRST."""
    with json_write_lock(store_path()):
        yield


def _write(doc: dict) -> None:
    atomic_write_json(store_path(), doc)


def _find(doc: dict, sid: str) -> dict:
    for s in doc["servers"]:
        if s["id"] == sid:
            return s
    raise NotFound("no such MCP server")


def get(sid: str) -> dict:
    if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
        raise NotFound("no such MCP server")
    return _find(read(), sid)


def list_servers() -> list[dict]:
    return list(read()["servers"])


def _bump(previous: float) -> float:
    now = time.time()
    return now if now > previous else previous + 1e-6


def _origin(raw: dict) -> dict:
    """A resolution's display-only provenance (#2353): the repository link and its commit. Set by
    the SERVER from a resolution (`mcp_jobs.resolution`), never from a client field."""
    url, commit = raw.get("url"), raw.get("commit")
    if (
        not isinstance(url, str)
        or not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit)
    ):
        raise RegistryError("a resolution's origin is malformed")
    return {"url": _text(url, "the origin", cap=URL_MAX), "commit": commit}


def create(payload: object, *, resolution: dict | None = None) -> dict:
    """Register a server. With ``resolution`` (a link's server-held ``{id, source, origin}``,
    #2353) the record takes that source, that origin and the id its review was staged under, in
    the same write; a ``source`` in the payload that differs from it is refused (Conflict)."""
    if not isinstance(payload, dict):
        raise RegistryError("a server is an object")
    unknown = set(payload) - {
        "name",
        "label",
        "source",
        "args",
        "settings",
        "project_settings",
        "projects",
        "probe",
    }
    if unknown:
        raise RegistryError("unknown server field")
    origin = None
    if resolution is not None:
        if resolution.get("update_of"):
            raise Conflict("that resolution updates an existing server; it registers nothing new")
        bound = _source(resolution["source"], resolved=True)
        if "source" in payload and _source(payload["source"], resolved=True) != bound:
            raise Conflict(
                "the source differs from the package the link resolved to; resolve the link again"
            )
        source, origin = bound, _origin(resolution["origin"])
        if not ID_RE.fullmatch(str(resolution.get("id") or "")):
            raise RegistryError("a resolution's server id is malformed")
    else:
        source = _source(payload.get("source"))
    transport = _transport(source)
    settings = _settings(payload.get("settings"), transport)
    with fence():
        doc = read()
        if len(doc["servers"]) >= SERVERS_MAX:
            raise RegistryError(f"at most {SERVERS_MAX} MCP servers")
        sid = secrets.token_hex(8) if resolution is None else resolution["id"]
        if any(s["id"] == sid for s in doc["servers"]):
            raise Conflict("a server was already registered from this resolution; resolve again")
        now = time.time()
        rec = {
            "id": sid,
            "name": _check_name(payload.get("name"), doc["servers"], None),
            "label": _text(payload.get("label", ""), "the label", cap=80),
            "source": source,
            "transport": transport,
            "args": _args(payload.get("args"), transport),
            "settings": settings,
            "project_settings": _project_settings(payload.get("project_settings"), settings),
            "projects": _projects(payload.get("projects")),
            "probe": _probe(payload.get("probe")),
            "enabled": True,
            "rev": 1,
            "installed": None,
            "created_at": now,
            "updated_at": now,
        }
        if origin is not None:
            rec["origin"] = origin
        doc["servers"].append(rec)
        _write(doc)
    return rec


#: Fields whose change alters what a launch receives: they move ``rev`` (and so stale every check).
_DELIVERY_FIELDS = ("source", "args", "settings", "probe")


def update(sid: str, payload: object, expected: float, *, resolution: dict | None = None) -> dict:
    """Change a server. With ``resolution`` (Update from source, #2427: the server-held ``{id,
    source, origin, update_of}`` of re-reading this server's own link) the source and origin are
    replaced together; the install then follows the new source (a new install, a new check)."""
    if not isinstance(payload, dict):
        raise RegistryError("an update is an object")
    if resolution is not None and "source" in payload:
        raise RegistryError(
            "an update from source takes its source from the link, not from a field"
        )
    unknown = set(payload) - {
        "name",
        "label",
        "source",
        "args",
        "settings",
        "project_settings",
        "projects",
        "probe",
        "enabled",
    }
    if unknown:
        raise RegistryError("unknown or read-only server field")
    from . import secrets_broker

    # The broker's fence FIRST, then this store's: the enable gate and every rev change are
    # decided in the same order delivery and settlement use.
    with secrets_broker.access_fence(), fence():
        doc = read()
        rec = _find(doc, sid)
        if rec["updated_at"] != expected:
            raise Conflict("the server changed since you loaded it")
        new = dict(rec)
        if resolution is not None:
            if resolution.get("update_of") != sid or resolution.get("id") != sid:
                raise Conflict("that resolution is not this server's update; read its link again")
            previous = resolution.get("previous") or {}
            if (rec["source"], rec.get("origin")) != (
                previous.get("source"),
                previous.get("origin"),
            ):
                raise Conflict("the server changed since its link was read; update it again")
            if not rec.get("origin"):
                raise RegistryError("this server was not added from a repository link")
            source = _source(resolution["source"], resolved=True)
            if _transport(source) != rec["transport"]:
                raise RegistryError("a server keeps its kind: a package stays a package")
            new["source"], new["origin"] = source, _origin(resolution["origin"])
        if "name" in payload:
            new["name"] = _check_name(payload["name"], doc["servers"], sid)
        if "label" in payload:
            new["label"] = _text(payload["label"], "the label", cap=80)
        if "source" in payload:
            # A Go module's source is never typed in (#2427): resending its own source unchanged
            # is not a change; a new version is a new repository-link resolution.
            same = payload["source"] == rec["source"]
            source = _source(payload["source"], resolved=same and rec["source"]["kind"] == "go")
            if _transport(source) != rec["transport"]:
                raise RegistryError(
                    "a server keeps its kind: remote stays remote, a package stays a package"
                )
            new["source"] = source
            if source != rec["source"]:
                # Provenance belongs to the source it was resolved for (#2353): it comes back only
                # through a new resolution, never by keeping a link beside another package.
                new.pop("origin", None)
        if "args" in payload:
            new["args"] = _args(payload["args"], rec["transport"])
        if "settings" in payload:
            new["settings"] = _settings(payload["settings"], rec["transport"])
        if "project_settings" in payload or "settings" in payload:
            new["project_settings"] = _project_settings(
                payload.get("project_settings", rec["project_settings"]), new["settings"]
            )
        if "probe" in payload:
            new["probe"] = _probe(payload["probe"])
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise RegistryError("enabled is true or false")
            new["enabled"] = payload["enabled"]
        if "projects" in payload:
            new["projects"] = _projects(payload["projects"])
            _enable_gate(new, new["projects"], rec)
        if any(new[f] != rec[f] for f in _DELIVERY_FIELDS):
            new["rev"] = rec["rev"] + 1
        new["updated_at"] = _bump(rec["updated_at"])
        doc["servers"] = [new if s["id"] == sid else s for s in doc["servers"]]
        _write(doc)
    return new


def delete(sid: str, expected: float) -> None:
    with fence():
        doc = read()
        rec = _find(doc, sid)
        if rec["updated_at"] != expected:
            raise Conflict("the server changed since you loaded it")
        doc["servers"] = [s for s in doc["servers"] if s["id"] != sid]
        doc["checks"] = [c for c in doc["checks"] if c["server"] != sid]
        _write(doc)


def set_installed(sid: str, installed: dict) -> dict:
    """Record a completed install (P1b). It belongs to a SOURCE, not a revision: editing a
    setting keeps the install; a changed source makes it not installed (`mcp_install.command`)."""
    from . import secrets_broker

    with secrets_broker.access_fence(), fence():
        doc = read()
        rec = _find(doc, sid)
        if rec["source"] != installed.get("source"):
            raise Conflict("the server's source changed while it was being installed")
        rec["installed"] = installed
        rec["updated_at"] = _bump(rec["updated_at"])
        _write(doc)
    return rec


# ---- resolution --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """What one scope resolves a server to NOW. ``values`` hold credential VALUES only when asked
    for (`resolve(..., with_values=True)`, the broker's delivery and the check); ``missing`` names
    the credential variables that resolve to nothing; ``digest`` identifies the resolved set
    (revision, source, plain values, each credential's record id + value revision) — never a
    value."""

    digest: str
    missing: tuple[str, ...]
    env: Mapping[str, str]
    headers: Mapping[str, str]
    sources: Mapping[str, str]  # variable → "own" | "shared" | "missing"
    #: The raw credential values (``with_values`` only) — for withholding server output.
    secret_values: tuple[str, ...] = ()


def _variables_records() -> list[dict]:
    from . import template_vars as store

    try:
        records, damaged = store._read(store.store_path())
    except Exception:  # noqa: BLE001 — unreadable or newer store: decide nothing
        raise RegistryError("the variables store cannot be read") from None
    if damaged:
        raise RegistryError("the variables store cannot be read")
    return records


def _holder(records: list[dict], name: str, scope: str) -> tuple[dict | None, str]:
    from . import template_vars as store

    globals_ = {r["name"]: r for r in records if store._is_global(r)}
    if scope == SHARED:
        rec = globals_.get(name)
        if rec is None or rec["kind"] != "secret" or rec.get("unset"):
            return None, "missing"
        return rec, "shared"
    for rec in records:
        if rec["scope"] != store.SCOPE_PROJECT or rec["project_id"] != scope:
            continue
        if rec["name"] != name:
            continue
        if rec.get("ref") == store.REF_GLOBAL:
            holder = globals_.get(name)
            if holder is None or holder["kind"] != "secret" or holder.get("unset"):
                return None, "missing"
            return holder, "shared"
        if rec["kind"] != "secret" or rec.get("unset"):
            return None, "missing"
        return rec, "own"
    return None, "missing"


def resolve(
    rec: dict, scope: str, *, records: list[dict] | None = None, with_values: bool = False
) -> Resolved:
    """Resolve server ``rec`` for ``scope`` (`SHARED` or a project id) from a strict read of the
    variables store (or the ``records`` the caller already read)."""
    from . import secrets_broker
    from . import template_vars as store

    if records is None:
        records = _variables_records()
    plain_over = rec["project_settings"].get(scope, {}) if scope != SHARED else {}
    parts: list = [rec["rev"], rec["source"], rec["args"]]
    if rec["transport"] == "stdio":
        # The installed artifact is part of what a check vouches for: a new install of the same
        # version can resolve a different closure, so it needs its own passing check. The same
        # review installed again (a retry) keeps the identity, and so the check.
        inst = rec.get("installed") or {}
        parts.append(["installed", inst.get("review_id"), inst.get("lock_digest")])
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    missing: list[str] = []
    sources: dict[str, str] = {}
    secret_values: list[str] = []
    for s in rec["settings"]:
        target = env if s["deliver"] == "env" else headers
        if s["kind"] == "plain":
            value = plain_over.get(s["key"], s["value"])
            parts.append(["plain", s["key"], value])
            target[s["key"]] = value
            continue
        holder, src = _holder(records, s["variable"], scope)
        sources[s["variable"]] = src
        if holder is None:
            missing.append(s["variable"])
            parts.append(["missing", s["key"], s["variable"]])
            continue
        parts.append(["credential", s["key"], s["prefix"], secrets_broker._marker(holder)])
        if with_values:
            value = store._decrypt(holder)
            if value is None:
                missing.append(s["variable"])
                sources[s["variable"]] = "missing"
                continue
            target[s["key"]] = s["prefix"] + value
            secret_values.append(value)
    digest = hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    return Resolved(
        digest=digest,
        missing=tuple(missing),
        env=env,
        headers=headers,
        sources=sources,
        secret_values=tuple(secret_values),
    )


def _passing(doc: dict, sid: str, rev: int, digest: str) -> dict | None:
    """The newest passing check of ``sid`` for exactly this revision and resolved set."""
    for c in reversed(doc["checks"]):
        if (
            c["server"] == sid
            and c["rev"] == rev
            and c["digest"] == digest
            and c["outcome"] == "passed"
        ):
            return c
    return None


def enabled_for(rec: dict, pid: str) -> bool:
    if not rec["enabled"]:
        return False
    return rec["projects"] == "all" or pid in rec["projects"]


def _enable_gate(new: dict, projects: str | list[str], old: dict) -> None:
    """Turning a project ON needs a passing check for its current resolved set. ``all`` is the
    operator's standing choice: each project then receives the server once its own check passes
    (delivery re-checks per launch)."""
    if projects == "all":
        return
    before = set(old["projects"]) if old["projects"] != "all" else set()
    added = [p for p in projects if p not in before]
    if not added:
        return
    doc = read()
    records = _variables_records()
    for pid in added:
        r = resolve(new, pid, records=records)
        if r.missing:
            raise RegistryError(f"{pid}: add the credentials first ({', '.join(r.missing)})")
        if _passing(doc, new["id"], new["rev"], r.digest) is None:
            raise RegistryError(f"{pid}: run a check that passes before enabling it")


def set_projects(sid: str, projects: object, expected: float) -> dict:
    """Enable the server for exactly these projects (or ``"all"``); `update` takes the broker's
    ``access_fence`` then this store's lock, so the gate reads the same state delivery does."""
    return update(sid, {"projects": projects}, expected)


# ---- check results -----------------------------------------------------------------------------


SUPERSEDED_DETAIL = "the server or its settings changed during the check"


def record_check(
    sid: str,
    scope: str,
    started: tuple[int, str],
    outcome: str,
    *,
    steps: list[dict],
    tools: list[str],
) -> dict:
    """Settle a check under ``access_fence`` → this lock. ``started`` is ``(rev, digest)`` read
    when the check began; if either moved, the result is ``superseded``, never ``passed``."""
    from . import secrets_broker

    if outcome not in ("passed", "failed"):
        raise ValueError(outcome)
    with secrets_broker.access_fence(), fence():
        doc = read()
        rec = _find(doc, sid)
        try:
            now_digest = resolve(rec, scope).digest
        except RegistryError:
            now_digest = None
        if (rec["rev"], now_digest) != started:
            # A superseded row keeps which steps ran, never what the server said: its output was
            # read against a set that is no longer current (withholding stays the first guard).
            outcome = "superseded"
            steps = [
                {"step": s.get("step", "?"), "ok": bool(s.get("ok")), "detail": SUPERSEDED_DETAIL}
                for s in steps
            ]
            tools = []
        row = {
            "server": sid,
            "scope": scope,
            "rev": started[0],
            "digest": started[1],
            "outcome": outcome,
            "at": time.time(),
            "steps": steps,
            "tools": [t for t in tools if TOOL_NAME_RE.fullmatch(t)][:TOOLS_MAX],
        }
        doc["checks"].append(row)
        mine = [c for c in doc["checks"] if c["server"] == sid]
        if len(mine) > CHECKS_PER_SERVER:
            drop = {id(c) for c in mine[: len(mine) - CHECKS_PER_SERVER]}
            doc["checks"] = [c for c in doc["checks"] if id(c) not in drop]
        _write(doc)
    return row


def checks(sid: str) -> list[dict]:
    return [c for c in read()["checks"] if c["server"] == sid]


# ---- delivery ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Delivery:
    """What the broker hands one launcher (as a private file): never logged, never returned."""

    payload: bytes
    digest: str


def _command(rec: dict) -> tuple[str, list[str]]:
    """The installed server's command line (P1b). A stdio server that is not installed has none."""
    from . import mcp_install

    return mcp_install.command(rec)


def deliver(sid: str, pid: str, *, check: tuple[int, str] | None = None) -> Delivery:
    """The resolved settings for a launch in project ``pid`` (or, for a check-only capability,
    the scope it was minted for: ``check = (rev, digest)``, the revision and resolved set the check
    started with — a set that changed since is refused, so a check is only ever delivered the
    credentials its withholding knows about). Called by the broker
    INSIDE its ``access_fence``. Raises ``secrets_broker.Refused`` with a fixed code."""
    from . import secrets_broker

    Refused = secrets_broker.Refused
    try:
        doc = read()
        rec = _find(doc, sid)
    except NotFound:
        raise Refused("server_unknown") from None
    except RegistryError:
        raise Refused("store_unavailable") from None
    if check is None:
        if not rec["enabled"]:
            raise Refused("server_disabled")
        if not enabled_for(rec, pid):
            raise Refused("not_enabled")
    elif rec["rev"] != check[0]:
        raise Refused("server_changed")
    try:
        r = resolve(rec, pid, with_values=True)
    except RegistryError:
        raise Refused("store_unavailable") from None
    if r.missing:
        raise Refused("credentials_missing")
    if check is not None and r.digest != check[1]:
        raise Refused("server_changed")
    if check is None and _passing(doc, sid, rec["rev"], r.digest) is None:
        raise Refused("check_required")
    body: dict = {"name": rec["name"], "transport": rec["transport"]}
    if rec["transport"] == "http":
        body["url"] = rec["source"]["url"]
        body["headers"] = dict(r.headers)
    else:
        try:
            exe, args = _command(rec)
        except RegistryError:
            raise Refused("not_installed") from None
        body["command"] = exe
        body["args"] = [*args, *rec["args"]]
        body["env"] = dict(r.env)
    return Delivery(payload=json.dumps(body).encode(), digest=r.digest)


def still_current(sid: str, pid: str, digest: str) -> bool:
    """Whether a delivered copy for ``pid`` is still what delivery would hand out now (the broker's
    reconcile pass). Unknowable → False: the copy goes."""
    try:
        doc = read()
        rec = _find(doc, sid)
        if not enabled_for(rec, pid):
            return False
        r = resolve(rec, pid)
        return not r.missing and r.digest == digest and _passing(doc, sid, rec["rev"], digest)
    except Exception:  # noqa: BLE001 — fail closed
        return False


def launch_servers(pid: str | None, secrets_env: Mapping[str, str] | None) -> list:
    """The `agent_tools.Server` entries for a new launch in project ``pid`` (`launch_specs`)."""
    from . import agent_tools

    out = []
    for spec in launch_specs(pid, secrets_env):
        server = agent_tools.project_server(spec["id"], spec["name"], secrets_env, spec["tools"])
        if server is not None:
            out.append(server)
    return out


def launch_specs(pid: str | None, secrets_env: Mapping[str, str] | None) -> list[dict]:
    """``{id, name, tools}`` for every server enabled in project ``pid`` whose check passed for
    the project's CURRENT resolved set, with that check's tool-name snapshot (Copilot's named
    list). Nothing without a secrets capability. Best-effort: never raises."""
    if not pid or not secrets_env:
        return []
    try:
        doc = read()
        if not doc["servers"]:
            return []
        records = _variables_records()
    except RegistryError:
        log.warning("MCP servers: the stores cannot be read; none added to this launch")
        return []
    out = []
    for rec in doc["servers"]:
        try:
            if not enabled_for(rec, pid):
                continue
            r = resolve(rec, pid, records=records)
            if r.missing:
                continue
            passed = _passing(doc, rec["id"], rec["rev"], r.digest)
            if passed is None:
                continue
            out.append({"id": rec["id"], "name": rec["name"], "tools": list(passed["tools"])})
        except Exception:  # noqa: BLE001 — one server's trouble never costs the launch
            log.warning("MCP servers: %s not added to this launch", rec.get("name"), exc_info=True)
    return out


# ---- what the operator sees --------------------------------------------------------------------


def public(rec: dict) -> dict:
    """A server as the API returns it: no value of any credential (there is none in the record)."""
    return {k: rec[k] for k in rec}


def status(rec: dict, project_ids: list[str]) -> dict:
    """Per-project state for the server page: credential sources, check state, enabled."""
    doc = read()
    records = _variables_records()
    rows = []
    for pid in project_ids:
        r = resolve(rec, pid, records=records)
        newest = next(
            (c for c in reversed(doc["checks"]) if c["server"] == rec["id"] and c["scope"] == pid),
            None,
        )
        passing = _passing(doc, rec["id"], rec["rev"], r.digest)
        if r.missing:
            state = "needs_credentials"
        elif passing is not None:
            state = "passed"
        elif newest is not None and newest["outcome"] == "failed" and newest["digest"] == r.digest:
            state = "failed"
        elif newest is not None:
            state = "stale"
        else:
            state = "not_run"
        own = [v for v, src in r.sources.items() if src == "own"]
        credentials = (
            "none_needed"
            if not r.sources
            else "missing"
            if r.missing
            else "own"
            if own
            else "shared"
        )
        rows.append(
            {
                "project_id": pid,
                "enabled": enabled_for(rec, pid),
                "credentials": credentials,
                "missing": list(r.missing),
                "check": state,
                "checked_at": (passing or newest or {}).get("at"),
                "delivered": enabled_for(rec, pid) and state == "passed",
            }
        )
    shared = resolve(rec, SHARED, records=records)
    return {
        "projects": rows,
        "shared": {
            "missing": list(shared.missing),
            "check": "passed" if _passing(doc, rec["id"], rec["rev"], shared.digest) else "not_run",
        },
    }
