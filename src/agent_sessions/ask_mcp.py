"""HQ sets up project MCP servers (#2163 P2): list them, and four cards the operator confirms.

HQ (a model) reads the Library's MCP servers the way the Library page does (``GET
/api/mcp/servers`` and each server's detail, as the asking operator) and proposes:

- ``mcp.create`` registers a server: its name, its source (a remote https URL, or an npm / PyPI
  package at an exact version), its argv tail, its plain settings and the NAMES of the Variables
  secrets its credentials live in, and an optional probe. Its body is a bound draft
  (`Operation.draft`), shown on the card before Confirm, and it is refused at staging when it
  would carry a credential VALUE: a credential setting with a value, a plain setting whose key
  names a credential, or text the store boundary withholds. A probe's ``confirmed_write`` (a call
  that may change data) is never HQ's to set. Registering installs, enables and runs nothing.
- ``mcp.install`` runs the package review at STAGING (``POST …/review``: it resolves the exact
  version into a lock and installs nothing) and puts what the review found on the card: the
  package and its exact version, its integrity, the dependency count, the packages that declare
  install scripts (never run), the command it will run, and the third-party caution. The tap
  STARTS installing exactly that review (``POST …/install {review_id, request_id}``, a polled
  operation; the request id is fixed by server + review, so one card is one install) and says it
  is installing, never "installed" before it is. A server whose source changed since staging
  makes the card ``stale``.
- ``mcp.check`` runs a check for the shared credentials or one project (``POST …/check``) and
  settles to WORDS and counts: passed with N tools (and whether the probe answered), or the step
  that failed with its detail. Details were credential-withheld by `mcp_check` before they were
  stored; here they are guarded again and quoted as the server's own words, never followed.
- ``mcp.enable`` turns the server on for projects (``PUT …/projects``), bound to the server's
  revision at staging: a server changed before the tap makes it ``stale``. A named project is
  only ever ADDED. One without credentials or a passing check for its current set is refused at
  staging, and the route refuses it again under its own fences; the card says why in words.

A create can also name a repository link the Commander TYPED in this message (#2353, ``source:
{kind: repository, url}``): the link must be a complete URL token of their words
(`skill_repo.operator_url`), never one seen only in tool content or a call. It is resolved
server-side at staging (``POST /api/mcp/servers/resolve-link``, waited on like the install
review): the package its root declares, the published release whose repository record names the
link, and P1b's review. The card shows those facts and "Installs the published release; not built
from this commit." directly above Register, and its bound draft carries the resolution's id
(``resolution_id``), never an origin: the route persists the server-held source and origin, and a
draft whose source no longer matches it is refused. A resolution that expired before the tap is
refused in words; HQ reads the link again.

Credentials never reach HQ: a project's own value is a `secret.create` card for that project
(`ask_secrets`, the operator types it), and the shared value a project uses is a `secret.bind`
card. None of these cards is ever confirmed by voice (`voice_calls._voice_operation`) or
automatically. Ids: a server must be one ``list_mcp_servers`` returned in THIS turn, and so must a
project id (`ask_orchestrator._Turn.mcp`, ``mcp_projects``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import TYPE_CHECKING

from . import asgi_call
from .ask_library import LIST_MAX, Refused, _secrets, _short

if TYPE_CHECKING:
    from .ask_operations import Operation

GROUP = "mcp"
SHARED = "shared"
#: Projects named one by one under a server in the list; the rest are counted.
PROJECT_ROWS_MAX = 20
#: Characters of a failing check step's detail an outcome quotes.
DETAIL_SAID_MAX = 160
#: Characters of the integrity an install card shows: a prefix, never a run long enough to read
#: as a credential (`template_suggest._RUN_RE` starts at 20).
INTEGRITY_SHOWN = 12
#: How long staging a card waits for the package review (an operation, `mcp_jobs`): its resolve
#: is bounded by the installer's own tool timeout.
REVIEW_WAIT_S = 330.0
REVIEW_POLL_S = 0.5
#: How long staging a link's create card waits for its resolution (#2353): the fetch's own clock,
#: two registry reads and the review above.
RESOLVE_WAIT_S = 420.0
#: Characters of a link's refusal the model is told (guarded whole first).
LINK_SAID_MAX = 400
#: Fixed namespace for an install's request id: the same server + review is the same operation,
#: so a second tap of one card never runs a second install.
_INSTALL_NS = uuid.UUID("5c1f0e7a-2163-4b1e-9c1a-6d0a9f3e2163")
THIRD_PARTY = "Third-party code: it runs as your user in every session that gets this server."
_CREATE_KEYS = frozenset({"action", "name", "label", "source", "args", "settings", "probe"})


def _ops():
    from . import ask_operations

    return ask_operations


def _digest(items) -> int:
    return _ops()._digest(items)


def _source_said(source: dict) -> str:
    """A server's source in words, without a URL path or query: ``npm pkg 1.2.3`` / ``https
    host``."""
    from urllib.parse import urlsplit

    kind = source.get("kind")
    if kind in ("npm", "pypi"):
        return (
            f"{'npm' if kind == 'npm' else 'PyPI'} {source.get('package')} {source.get('version')}"
        )
    try:
        host = urlsplit(str(source.get("url") or "")).hostname or "?"
    except ValueError:
        host = "?"
    return f"remote {host}"


def _installed(rec: dict) -> bool:
    if rec.get("transport") == "http":
        return True
    inst = rec.get("installed")
    return isinstance(inst, dict) and inst.get("source") == rec.get("source")


# ---- reads ---------------------------------------------------------------------------------------


async def _servers(app, operator) -> list[dict]:
    r = await asgi_call.call(app, operator, "GET", "/api/mcp/servers", None)
    if r.status != 200 or not isinstance(r.body, dict) or r.truncated:
        raise Refused("the MCP server list could not be read just now", "not_available")
    return [
        s
        for s in r.body.get("servers") or []
        if isinstance(s, dict) and isinstance(s.get("id"), str)
    ]


async def _detail(app, operator, sid: str) -> dict | None:
    """``{server, status, checks}`` as the server page reads it, or None when it is gone."""
    r = await asgi_call.call(app, operator, "GET", f"/api/mcp/servers/{sid}", None)
    if r.status == 404:
        return None
    if r.status != 200 or not isinstance(r.body, dict) or r.truncated:
        raise Refused("that MCP server could not be read just now", "not_available")
    return r.body


async def _project_names(app, operator) -> dict[str, str]:
    from .ask_secrets import _project_names

    return await _project_names(app, operator)


def _project_row(p: dict, names: dict[str, str], secrets: list[str]) -> dict:
    return {
        "project_id": p["project_id"],
        "project": _short(names.get(p["project_id"], p["project_id"]), 120, secrets),
        "enabled": bool(p.get("enabled")),
        # own / shared / missing / none_needed: where its credentials come from.
        "credentials": str(p.get("credentials") or ""),
        "missing": [str(m) for m in p.get("missing") or []][:16],
        # passed / failed / stale / not_run / needs_credentials
        "check": str(p.get("check") or ""),
    }


def _setting_row(s: dict, secrets: list[str]) -> dict:
    if s.get("kind") == "credential":
        return {"key": s.get("key"), "kind": "credential", "variable": s.get("variable")}
    return {"key": s.get("key"), "kind": "plain", "value": _short(s.get("value"), 200, secrets)}


async def list_mcp_servers(app, operator) -> tuple[dict, dict, dict]:
    """The Library's MCP servers as the model may see them, with the projects they can be
    enabled for. Returns ``(result, known servers, known projects)``. A server record holds no
    credential value (only Variables names), and every text is guarded whole before it is cut."""
    rows_in = await _servers(app, operator)
    names = await _project_names(app, operator)
    secrets = await asyncio.to_thread(_secrets)
    servers, known, seen_projects = [], {}, {}
    for s in rows_in[:LIST_MAX]:
        detail = await _detail(app, operator, s["id"])
        if detail is None:
            continue
        status = detail.get("status") if isinstance(detail.get("status"), dict) else {}
        rows = [p for p in status.get("projects") or [] if isinstance(p, dict)]
        for p in rows:
            seen_projects[p["project_id"]] = names.get(p["project_id"], p["project_id"])
        notable = [
            p
            for p in rows
            if p.get("enabled")
            or p.get("credentials") == "own"
            or p.get("check") in ("passed", "failed", "stale")
        ]
        others = [p for p in rows if p not in notable[:PROJECT_ROWS_MAX]]
        tally: dict[str, int] = {}
        for p in others:
            tally[str(p.get("credentials") or "")] = tally.get(str(p.get("credentials")), 0) + 1
        enabled = s.get("projects")
        state = s.get("state") if isinstance(s.get("state"), dict) else {}
        shared = status.get("shared") if isinstance(status.get("shared"), dict) else {}
        probe = s.get("probe") if isinstance(s.get("probe"), dict) else None
        servers.append(
            {
                "id": s["id"],
                "name": _short(s.get("name"), 32, secrets),
                "label": _short(s.get("label"), 80, secrets),
                "source": _short(_source_said(s.get("source") or {}), 200, secrets),
                "installed": _installed(s),
                "tools": int(s.get("tool_count") or 0),
                "switched_on": bool(s.get("enabled")),
                "enabled_for": "all"
                if enabled == "all"
                else [_short(names.get(p, p), 120, secrets) for p in enabled or []],
                # ready / needs_credentials / needs_check / failing / not_enabled / disabled
                "state": str(state.get("kind") or ""),
                "settings": [_setting_row(x, secrets) for x in s.get("settings") or []],
                "probe": probe.get("tool") if probe else None,
                "shared_credentials": {
                    "missing": [str(m) for m in shared.get("missing") or []][:16],
                    "check": str(shared.get("check") or ""),
                },
                "projects": [_project_row(p, names, secrets) for p in notable[:PROJECT_ROWS_MAX]],
                # The projects not listed one by one: off, never checked, by credential source.
                "other_projects": tally,
            }
        )
        known[s["id"]] = {"name": str(s.get("name") or "")}
    projects = [
        {"id": pid, "name": _short(name, 120, secrets)}
        for pid, name in sorted(seen_projects.items())
    ]
    if not servers:
        # No server yet: the projects a new one could be enabled for, from the project list.
        r = await asgi_call.call(app, operator, "GET", "/api/projects", None)
        rows = (r.body or {}).get("projects") if isinstance(r.body, dict) else None
        for p in rows or []:
            if isinstance(p, dict) and isinstance(p.get("id"), str) and not p.get("archived"):
                seen_projects[p["id"]] = str(p.get("name") or p["id"])
        projects = [
            {"id": pid, "name": _short(n, 120, secrets)} for pid, n in sorted(seen_projects.items())
        ]
    return {"servers": servers, "projects": projects}, known, dict(seen_projects)


# ---- facts ---------------------------------------------------------------------------------------


async def facts(op: Operation, target: str, fields: dict, app, operator) -> dict | None:
    """What an MCP card shows and binds, read the way the server page reads it. None when the
    server (or the project a check names) is gone."""
    if op.id == "mcp.create":
        return {}
    detail = await _detail(app, operator, target)
    if detail is None or not isinstance(detail.get("server"), dict):
        return None
    rec = detail["server"]
    out: dict = {"name": str(rec.get("name") or "")}
    if op.id == "mcp.install":
        # The source the review was run for: another source since is `stale`.
        out["source_rev"] = _source_rev(rec.get("source"))
    elif op.id == "mcp.check":
        out["revision"] = int(rec.get("rev") or 0)
        scope = fields.get("scope")
        if scope != SHARED:
            names = await _project_names(app, operator)
            if scope not in names:
                return None
    else:  # mcp.enable: the revision its project list was BUILT from (Hermes on #2208), never a
        # later one re-read here: the route's expected_updated_at at the tap is checked against it.
        out["revision"] = fields.get("based_on") or _revision(rec)
    return out


# ---- staging -------------------------------------------------------------------------------------


def _never(msg: str) -> Refused:
    return Refused(f"refused: {msg}", "never")


def _create_payload(args: dict, servers: list[dict], *, link: bool = False) -> dict:
    """The server a create card registers, validated by the registry's own rules without writing.
    Raises ``Refused``: ``never`` for anything that would carry a credential's value. ``link``: the
    source is a repository link resolved later (#2353), so it is left out here and the rest is
    checked as a package server's."""
    from . import mcp_registry, template_suggest

    extra = set(args) - _CREATE_KEYS
    if extra & {"projects", "project_settings"}:
        raise Refused(
            "refused: registering a server enables it nowhere — enable is its own card "
            "(action enable)",
            "bad_arguments",
        )
    if "value" in extra:
        raise _never("HQ never carries a credential's value — the operator types it on a card")
    if extra - {"id", "scope", "kind", "description", "project", "projects"}:
        raise Refused(f"refused: create takes no {sorted(extra)[0]}", "bad_arguments")
    settings = args.get("settings")
    for s in settings if isinstance(settings, list) else []:
        if not isinstance(s, dict):
            continue
        if s.get("kind") == "credential" and "value" in s:
            raise _never(
                "a credential setting names a Variables secret and never holds its value — the "
                "operator types the value on a credential card"
            )
        key = s.get("key")
        if (
            s.get("kind", "plain") == "plain"
            and isinstance(key, str)
            and template_suggest._is_cred_key(key)
            and s.get("value")
        ):
            raise _never(
                f"{key} looks like a credential: make it kind credential naming a Variables "
                "secret, and the operator types its value on a credential card"
            )
    probe = args.get("probe")
    if isinstance(probe, dict) and "confirmed_write" in probe:
        raise _never(
            "only the operator confirms a probe that may change data, in the Library; propose a "
            "read-only tool"
        )
    try:
        source = None if link else mcp_registry._source(args.get("source"))
        transport = "stdio" if source is None else mcp_registry._transport(source)
        payload = {
            "name": mcp_registry._check_name(args.get("name"), servers, None),
            "label": mcp_registry._text(args.get("label", ""), "the label", cap=80),
            **({"source": source} if source is not None else {}),
            "args": mcp_registry._args(args.get("args"), transport),
            "settings": mcp_registry._settings(settings, transport),
        }
        checked_probe = mcp_registry._probe(probe)
    except mcp_registry.RegistryError as e:
        raise Refused(f"refused: {e}", "bad_arguments") from None
    if checked_probe is not None:
        payload["probe"] = {"tool": checked_probe["tool"], "arguments": checked_probe["arguments"]}
    return payload


def _credentials_of(payload: dict) -> list[str]:
    return [s["variable"] for s in payload["settings"] if s["kind"] == "credential"]


def _describe_create(f: dict, x: dict) -> str:
    creds = f.get("credentials") or ""
    plain = int(f.get("plain") or 0)
    origin = f.get("origin_said")
    parts = [
        f"Registers the MCP server “{f['server_name']}”: the {f.get('source_said') or ''} that "
        f"{origin} declares at {f.get('origin_commit') or ''}"
        if origin
        else f"Registers the MCP server “{f['server_name']}” ({f.get('source_said') or ''})"
    ]
    said = []
    if plain:
        said.append(f"{plain} plain setting{'s' if plain != 1 else ''}")
    if creds:
        said.append(f"credentials from the Variables secrets {creds}")
    head = parts[0] + (f" with {' and '.join(said)}" if said else "") + "."
    tail = (
        " It installs, enables and runs nothing yet: next come install, credentials, a check, "
        "then enabling it for projects."
        if f.get("package")
        else " It enables and runs nothing yet: next come credentials, a check, then enabling "
        "it for projects."
    )
    return head + tail


def _created(status: int, body: object, x: dict) -> tuple[str, str]:
    if 200 <= status < 300:
        return _ops().DONE, "Registered. Nothing is installed or switched on yet."
    return _ops()._plain(status, body, "")


def _not_created(status: int, detail: str, x: dict) -> str:
    if status == 404 and "resolve the link again" in (detail or ""):
        # A link's resolution is held in memory for a while (#2353): HQ reads the link again.
        return (
            "Not registered: the link's reading is no longer held (it expired, or BattleLab "
            "restarted). Ask HQ to read the link again. Nothing changed."
        )
    return f"Not registered: {detail or 'refused'}. Nothing changed."


def _scripts_said(names: list[str]) -> str:
    names = [str(n) for n in names]
    if not names:
        return ""
    shown = ", ".join(names[:3])
    more = len(names) - 3
    return shown + (f" and {more} more" if more > 0 else "")


def _describe_install(f: dict, x: dict) -> str:
    deps = int(f.get("dependencies") or 0)
    scripts = f.get("scripts") or ""
    said = (
        f"Installs {f['package']} {f['version']} exactly ({f.get('integrity') or 'no digest'}…) "
        f"with {deps} locked dependenc{'ies' if deps != 1 else 'y'}; install scripts never run"
        + (f" (declared by {scripts})" if scripts else "")
        + f". It will run: {f.get('runs') or 'its command'}. "
        + THIRD_PARTY
    )
    return said


def install_request_id(sid: str, review_id: str) -> str:
    return str(uuid.uuid5(_INSTALL_NS, f"{sid}:{review_id}"))


def _installed_said(status: int, body: object, x: dict) -> tuple[str, str]:
    """The install runs as an operation (`mcp_jobs`): the tap STARTS it. Say so, never
    "installed" before it is."""
    if status == 202 and isinstance(body, dict):
        if body.get("state") == "failed":
            return _ops().FAILED, f"Not installed: {_detail_said(str(body.get('error') or ''))}"
        if body.get("state") == "done":
            return _ops().DONE, "Installed. Run a check before enabling it."
        return (
            _ops().DONE,
            "Installing now: it downloads the reviewed packages and takes a few minutes. The "
            "server shows as installed in Library → MCP servers when it is done; run a check "
            "then.",
        )
    return _ops()._plain(status, body, "")


def _check_outcome(status: int, body: object, x: dict) -> tuple[str, str]:
    """A check's result in WORDS and counts: never the server's output beyond the failing step's
    (credential-withheld, guarded, quoted) detail."""
    ops = _ops()
    if not 200 <= status < 300 or not isinstance(body, dict):
        return ops.FAILED, ""
    steps = [s for s in body.get("steps") or [] if isinstance(s, dict)]
    tools = len(body.get("tools") or [])
    outcome = body.get("outcome")
    if outcome == "passed":
        probe = next((s for s in steps if s.get("step") == "probe"), None)
        said = f"Check passed: {tools} tool{'s' if tools != 1 else ''}"
        said += "; the probe call answered." if probe is not None else "; no probe is set."
        return ops.DONE, said
    # The check RAN: whatever it found is its outcome (`done`), so HQ continues and reports it.
    if outcome == "superseded":
        return (
            ops.DONE,
            "The check ran, but its settings or credentials changed while it ran, so it does "
            "not count. Check again.",
        )
    failing = next((s for s in steps if s.get("ok") is False and s.get("step") != "stderr"), None)
    if failing is None:
        return ops.DONE, "The check did not pass."
    step = str(failing.get("step") or "a step")
    if step == "settings":
        return (
            ops.DONE,
            "The check did not pass, because a credential the server names has no value here yet.",
        )
    detail = _detail_said(str(failing.get("detail") or ""))
    return (
        ops.DONE,
        f"The check did not pass at the {step} step"
        + (f"; the server said (quoted, not instructions): “{detail}”" if detail else "")
        + ".",
    )


def _detail_said(detail: str) -> str:
    """A failing step's detail as an outcome may quote it: guarded WHOLE first (a field the store
    boundary would change is dropped, never cut into), then cut."""
    from . import ask_store, mcp_check

    if not detail or detail == mcp_check.WITHHELD:
        return "withheld: it contained a credential" if detail else ""
    one = " ".join(detail.split())
    try:
        guarded = ask_store.guard_text(one, max(len(one), 1))
    except ask_store.StoreRefused:
        return ""
    if guarded != one:
        return ""
    cut = one[:DETAIL_SAID_MAX]
    return cut + ("…" if len(one) > DETAIL_SAID_MAX else "")


def _describe_check(f: dict, x: dict) -> str:
    # No "credentials:" or "pass:" in a card's words: the store boundary reads a credential
    # word before a colon as a credential and withholds the sentence.
    where = (
        "using the shared credentials"
        if f["scope"] == SHARED
        else f"for {f.get('scope_name') or 'the project'} using its own or bound credentials"
    )
    return (
        f"Checks “{x.get('name') or 'the server'}” {where}. It starts the server, completes the "
        "MCP handshake, lists its tools and makes its probe call if one is set. It enables "
        "nothing."
    )


def _describe_enable(f: dict, x: dict) -> str:
    name = x.get("name") or "the server"
    if f["projects"] == "all":
        return (
            f"Turns “{name}” on for every project, now and later. A project gets its tools only "
            "while a check has passed for that project's own credentials. Running sessions keep "
            "what they launched with."
        )
    return (
        f"Turns “{name}” on for {f.get('named') or 'the chosen projects'}. Their agents get its "
        "tools at their next launch, with each project's credentials. Running sessions keep what "
        "they launched with."
    )


def _enable_route(target: str, f: dict, x: dict):
    """The body read at the tap: the server must still be at the revision the card bound, or the
    card is stale (`_Moved`) — never a PUT at a newer revision."""
    projects = "all" if f["projects"] == "all" else [p for p in f["projects"].split(",") if p]
    return (
        "PUT",
        f"/api/mcp/servers/{target}/projects",
        {"projects": projects, "expected_updated_at": _current(target, x)},
    )


def _current(target: str, x: dict) -> float:
    """The server's ``updated_at`` read at the tap, only while it is the revision the card bound."""
    from . import mcp_registry

    try:
        rec = mcp_registry.get(target)
    except mcp_registry.RegistryError:
        raise _ops()._Moved("it no longer exists") from None
    if _digest([repr(rec["updated_at"])]) != x.get("revision"):
        raise _ops()._Moved("it was changed elsewhere")
    return rec["updated_at"]


def _enable_failure(status: int, detail: str, x: dict) -> str:
    """The route's refusal in words: a project id it names is said as the project's name."""
    from . import projects

    said = detail or "refused"
    with contextlib.suppress(Exception):  # unreadable projects: the words stay the route's own
        for pid, p in projects.load().items():
            said = said.replace(f"{pid}:", f"{p.name}:")
    return f"Not switched on: {said}. Nothing changed."


def mcp_ops() -> tuple[Operation, ...]:
    Operation = _ops().Operation  # noqa: N806 — the dataclass, imported late (a cycle otherwise)
    return (
        Operation(
            "mcp.create",
            GROUP,
            "create",
            "Add MCP server",
            "Register",
            "reversible",
            "none",
            ("server_name",),
            lambda t, f, x: ("POST", "/api/mcp/servers", (f.get("_content") or {}).get("server")),
            _describe_create,
            _created,
            _not_created,
            draft=True,
            follow_up=True,
        ),
        Operation(
            "mcp.install",
            GROUP,
            "install",
            "Install MCP server",
            "Install",
            "external",
            GROUP,
            (),
            lambda t, f, x: (
                "POST",
                f"/api/mcp/servers/{t}/install",
                {"review_id": f["review_id"], "request_id": install_request_id(t, f["review_id"])},
            ),
            _describe_install,
            _installed_said,
            lambda st, d, x: f"Not installed: {d or 'refused'}.",
            follow_up=True,
        ),
        Operation(
            "mcp.check",
            GROUP,
            "check",
            "Check MCP server",
            "Run check",
            "external",
            GROUP,
            (),
            lambda t, f, x: ("POST", f"/api/mcp/servers/{t}/check", {"scope": f["scope"]}),
            _describe_check,
            _check_outcome,
            lambda st, d, x: f"The check could not run: {d or 'refused'}.",
            follow_up=True,
        ),
        Operation(
            "mcp.enable",
            GROUP,
            "enable",
            "Turn MCP server on",
            "Turn on",
            "external",
            GROUP,
            (),
            _enable_route,
            _describe_enable,
            lambda st, b, x: _ops()._plain(
                st, b, "Switched on. New launches in those projects get its tools."
            ),
            _enable_failure,
        ),
    )


def _project_ids(raw: object, known_projects: dict) -> list[str]:
    if not isinstance(raw, list) or not raw or not all(isinstance(p, str) for p in raw):
        raise Refused(
            'refused: projects is "all" or a list of project ids list_mcp_servers returned in '
            "this turn",
            "bad_arguments",
        )
    unknown = [p for p in raw if p not in known_projects]
    if unknown:
        raise Refused(
            "refused: use project ids list_mcp_servers returned in this turn", "unknown_target"
        )
    return list(dict.fromkeys(raw))


async def prepare(
    action: object,
    args: dict,
    *,
    known: dict,
    known_projects: dict,
    app,
    operator,
    question: str | None = None,
) -> dict:
    """What `propose_mcp` stages: ``{op, target, title, fields, draft?}``. Raises ``Refused``.
    ``question``: the Commander's typed message this turn (None on a call or a continuation),
    the only place a repository link may come from (#2353)."""
    op = _ops().by_action(GROUP, action) if isinstance(action, str) else None
    if op is None:
        raise Refused("action must be create, install, check or enable", "bad_arguments")
    if "value" in args or "values" in args:
        raise _never("HQ never carries a credential's value — the operator types it on a card")
    source_arg = args.get("source")
    if (
        op.id == "mcp.create"
        and isinstance(source_arg, dict)
        and (source_arg.get("kind") == "repository")
    ):
        return await _prepare_link(op, args, question, app, operator)
    if op.id == "mcp.create":
        payload = _create_payload(args, await _servers(app, operator))
        source = payload["source"]
        creds = _credentials_of(payload)
        fields = {
            "server_name": payload["name"],
            "source_said": _source_said(source),
            "package": source["kind"] in ("npm", "pypi"),
            "plain": sum(1 for s in payload["settings"] if s["kind"] == "plain"),
            "credentials": ", ".join(creds),
        }
        return {
            "op": op,
            "target": "",
            "title": payload["name"],
            "fields": fields,
            "draft": {"server": payload},
        }
    sid = args.get("id")
    if not isinstance(sid, str) or sid not in known:
        raise Refused(
            "refused: use a server id list_mcp_servers returned in this turn", "unknown_target"
        )
    detail = await _detail(app, operator, sid)
    if detail is None:
        raise Refused("refused: that MCP server no longer exists", "not_available")
    rec = detail["server"]
    title = str(rec.get("name") or known[sid].get("name") or sid)
    if op.id == "mcp.install":
        # The card shows THIS source; the review must be of it, and staging re-reads it: a source
        # that moved anywhere before the card is staged refuses (Hermes on #2208), one that moves
        # after staging makes the card stale at the tap (`changed`'s ``source_rev``).
        return {"op": op, "target": sid, "title": title,
                "fields": await _review(app, operator, rec),
                "expected": {"source_rev": _source_rev(rec.get("source"))}}  # fmt: skip
    if op.id == "mcp.check":
        scope = args.get("scope")
        if scope == SHARED:
            return {"op": op, "target": sid, "title": title, "fields": {"scope": SHARED}}
        if not isinstance(scope, str) or scope not in known_projects:
            raise Refused(
                'refused: scope is "shared" or a project id list_mcp_servers returned in this turn',
                "unknown_target",
            )
        fields = {"scope": scope, "scope_name": str(known_projects[scope])[:120]}
        return {"op": op, "target": sid, "title": title, "fields": fields}
    fields = _enable_fields(args, rec, detail, known_projects)
    return {"op": op, "target": sid, "title": title, "fields": fields}


# ---- a create from a repository link (#2353) ----------------------------------------------------


async def _prepare_link(op, args: dict, question: str | None, app, operator) -> dict:
    """A create card for the package a typed repository link declares, resolved server-side NOW;
    the card binds the resolution's id, never an origin or a package HQ chose."""
    from . import skill_repo

    source = args["source"]
    if set(source) - {"kind", "url"}:
        raise Refused(
            "refused: a repository source is {kind: repository, url}; the package and its version "
            "come from the repository, never from HQ",
            "bad_arguments",
        )
    if question is None:
        raise Refused(
            "refused: a repository is read only from a link the Commander typed in HQ", "not_typed"
        )
    try:
        typed = skill_repo.operator_url(source.get("url"), question)
    except skill_repo.Refused as e:
        raise Refused(str(e), e.code) from None
    payload = _create_payload(args, await _servers(app, operator), link=True)
    res = await _await_resolution(app, operator, typed)
    resolved = res.get("source")
    origin = res.get("origin") if isinstance(res.get("origin"), dict) else {}
    facts = res.get("facts") if isinstance(res.get("facts"), dict) else {}
    if not isinstance(resolved, dict) or not isinstance(origin.get("commit"), str):
        raise Refused("refused: the link's reading came back incomplete; try again", "link_refused")
    payload["source"] = resolved
    kind = "npm package" if resolved.get("kind") == "npm" else "PyPI package"
    fields = {
        "server_name": payload["name"],
        "source_said": f"{kind} {resolved.get('package')} {resolved.get('version')}",
        "package": True,
        "plain": sum(1 for s in payload["settings"] if s["kind"] == "plain"),
        "credentials": ", ".join(_credentials_of(payload)),
        "origin_said": str(facts.get("repository") or ""),
        "origin_commit": origin["commit"][:7],
    }
    draft = {
        "server": {**payload, "resolution_id": str(res.get("resolution_id") or "")},
        "link": _link_view(res, facts, origin),
    }
    return {"op": op, "target": "", "title": payload["name"], "fields": fields, "draft": draft}


def _integrity_shown(integrity: str) -> str:
    """ "sha512-…" (npm) or "sha256:…" (PyPI): the algorithm and a short prefix of the digest."""
    cut = min((i for i in (integrity.find("-"), integrity.find(":")) if i >= 0), default=-1)
    return integrity[: cut + 1 + INTEGRITY_SHOWN] if 0 < cut <= 8 else ""


def _link_view(res: dict, facts: dict, origin: dict) -> dict:
    """What the link card shows of its resolution: the repository, the package it declares, the
    registry record that names it, then the release P1b reviewed. Display only: the tap builds
    nothing from it."""
    declared = facts.get("declared") if isinstance(facts.get("declared"), dict) else {}
    release = facts.get("release") if isinstance(facts.get("release"), dict) else {}
    review = res.get("review") if isinstance(res.get("review"), dict) else {}
    scripts = [str(x) for x in review.get("install_scripts_declared") or []]
    return {
        "repository": str(facts.get("repository") or ""),
        "commit": origin["commit"][:7],
        "manifest": str(declared.get("manifest") or ""),
        "kind": str(declared.get("kind") or ""),
        "package": str(declared.get("package") or ""),
        "version": str(declared.get("version") or ""),
        "registry": str(release.get("registry") or ""),
        "record_field": str(release.get("record_field") or ""),
        "names": str(release.get("names") or ""),
        "integrity": _integrity_shown(str(review.get("integrity") or "")),
        "dependencies": int(review.get("dependencies") or 0),
        "install_scripts": _scripts_said(scripts or list(release.get("install_scripts") or [])),
        "runs": str(review.get("runs") or "")[:120],
        "license": str(release.get("license") or review.get("license") or "")[:80],
        "disclaimer": str(res.get("disclaimer") or ""),
    }


def _link_said(text: str) -> str:
    """A link's refusal as the model may be told it: guarded WHOLE (a field the store boundary
    would change is dropped), then cut."""
    from . import ask_store

    one = " ".join(text.split())
    if not one:
        return ""
    try:
        guarded = ask_store.guard_text(one, max(len(one), 1))
    except ask_store.StoreRefused:
        return ""
    if guarded != one:
        return ""
    return one[:LINK_SAID_MAX] + ("…" if len(one) > LINK_SAID_MAX else "")


async def _await_resolution(app, operator, url: str) -> dict:
    """Start reading the link (an operation, `mcp_jobs`) and wait for its result in process. A
    Stop of the turn, or the wait running out, stops the read."""
    rid = str(uuid.uuid4())
    r = await asgi_call.call(
        app, operator, "POST", "/api/mcp/servers/resolve-link", {"url": url, "request_id": rid}
    )
    if r.status != 202 or not isinstance(r.body, dict):
        why = _link_said(r.detail or "") or "it could not be read"
        raise Refused(f"refused: the link was not read: {why}", "link_refused")
    deadline = time.monotonic() + RESOLVE_WAIT_S
    op = r.body
    try:
        while op.get("state") == "running":
            if time.monotonic() > deadline:
                raise Refused("refused: reading the link is taking too long; try again", "busy")
            await asyncio.sleep(REVIEW_POLL_S)
            r = await asgi_call.call(app, operator, "GET", f"/api/mcp/operations/{rid}", None)
            if r.status != 200 or not isinstance(r.body, dict):
                raise Refused("refused: the link's reading was lost; try again", "link_refused")
            op = r.body
    except (asyncio.CancelledError, Refused):
        with contextlib.suppress(Exception):
            await asgi_call.call(app, operator, "POST", f"/api/mcp/operations/{rid}/stop", None)
        raise
    if op.get("state") != "done" or not isinstance(op.get("result"), dict):
        why = _link_said(str(op.get("error") or "")) or "it could not be read"
        raise Refused(f"refused: {why}. Nothing was registered", "link_refused")
    return op["result"]


async def _review(app, operator, rec: dict) -> dict:
    """Run the package review now (it installs nothing) and return the card's fields."""
    if rec.get("transport") != "stdio":
        raise Refused(
            "refused: a remote server installs nothing — propose a check for it", "bad_arguments"
        )
    if _installed(rec):
        raise Refused(
            "refused: this server is already installed for its current source", "installed"
        )
    rv = await _await_review(app, operator, rec["id"])
    if rv.get("source") != rec.get("source"):
        raise Refused(
            "refused: the server's source changed while it was being reviewed; read it again "
            "with list_mcp_servers. Nothing was installed",
            "stale",
        )
    source = rec.get("source") or {}
    integrity = str(rv.get("integrity") or "")
    # "sha512-…" (npm) or "sha256:…" (PyPI): the algorithm and a short prefix of the digest.
    cut = min((i for i in (integrity.find("-"), integrity.find(":")) if i >= 0), default=-1)
    shown = integrity[: cut + 1 + INTEGRITY_SHOWN] if 0 < cut <= 8 else ""
    return {
        "review_id": str(rv.get("review_id") or ""),
        "package": f"{'npm' if source.get('kind') == 'npm' else 'PyPI'} {source.get('package')}",
        "version": str(source.get("version") or ""),
        "integrity": shown,
        "dependencies": int(rv.get("dependencies") or 0),
        "scripts": _scripts_said(list(rv.get("install_scripts_declared") or [])),
        "runs": str(rv.get("runs") or "")[:120],
    }


async def _await_review(app, operator, sid: str) -> dict:
    """Start the review operation and wait for its result (in process: no proxy in between)."""
    rid = str(uuid.uuid4())
    r = await asgi_call.call(
        app, operator, "POST", f"/api/mcp/servers/{sid}/review", {"request_id": rid}
    )
    if r.status != 202 or not isinstance(r.body, dict):
        why = _detail_said(r.detail or "") or "it could not be reviewed"
        raise Refused(f"refused: the review failed: {why}", "review_failed")
    deadline = time.monotonic() + REVIEW_WAIT_S
    op = r.body
    while op.get("state") == "running":
        if time.monotonic() > deadline:
            raise Refused("refused: the review is taking too long; try again", "review_failed")
        await asyncio.sleep(REVIEW_POLL_S)
        r = await asgi_call.call(app, operator, "GET", f"/api/mcp/operations/{rid}", None)
        if r.status != 200 or not isinstance(r.body, dict):
            raise Refused("refused: the review was lost; try again", "review_failed")
        op = r.body
    if op.get("state") != "done" or not isinstance(op.get("result"), dict):
        why = _detail_said(str(op.get("error") or "")) or "it could not be reviewed"
        raise Refused(f"refused: the review failed: {why}", "review_failed")
    return op["result"]


def _enable_fields(args: dict, rec: dict, detail: dict, known_projects: dict) -> dict:
    wanted = args.get("projects")
    current = rec.get("projects")
    if wanted == "all":
        if current == "all":
            raise Refused("refused: it is already on for every project", "unchanged")
        return {"projects": "all", "named": "every project", "based_on": _revision(rec)}
    if current == "all":
        raise Refused("refused: it is already on for every project", "unchanged")
    pids = _project_ids(wanted, known_projects)
    rows = {
        p["project_id"]: p
        for p in (detail.get("status") or {}).get("projects") or []
        if isinstance(p, dict)
    }
    added = [p for p in pids if p not in (current or [])]
    if not added:
        raise Refused("refused: it is already on for those projects", "unchanged")
    why = []
    for pid in added:
        row = rows.get(pid)
        name = str(known_projects.get(pid) or pid)
        if row is None:
            why.append(f"{name} is archived or gone")
        elif row.get("credentials") == "missing":
            missing = ", ".join(row.get("missing") or []) or "its credentials"
            why.append(f"{name} needs {missing} first (a credential card)")
        elif row.get("check") != "passed":
            why.append(f"{name} needs a check that passes first")
    if why:
        raise Refused("refused: " + "; ".join(why), "not_ready")
    named = ", ".join(str(known_projects.get(p) or p) for p in added)
    # The list REPLACES the server's projects, so the card binds the revision it was built from
    # (`based_on`): a project enabled after this read makes the card stale, never removed.
    return {
        "projects": ",".join([*(current or []), *added]),
        "named": named[:300],
        "based_on": _revision(rec),
    }


def _source_rev(source: object) -> str:
    return _digest([json.dumps(source, sort_keys=True)])


def _revision(rec: dict) -> str:
    return _digest([repr(rec.get("updated_at"))])


def content_view(draft: dict) -> dict:
    """What a create card shows of its bound draft (the operator's browser only, never the
    model): the server exactly as it will be registered, and for a repository link (#2353) what
    the link resolved to, with the disclaimer directly above Register."""
    if isinstance(draft.get("link"), dict):
        server = {k: v for k, v in (draft.get("server") or {}).items() if k != "resolution_id"}
        return {
            "kind": "mcp_link",
            "link": draft["link"],
            "server": json.dumps(server, indent=2, ensure_ascii=False),
        }
    return {
        "kind": "files",
        "files": [
            {
                "path": "mcp-server.json",
                "text": json.dumps(draft.get("server") or {}, indent=2, ensure_ascii=False),
            }
        ],
    }


__all__ = ["GROUP", "content_view", "facts", "list_mcp_servers", "mcp_ops", "prepare"]
