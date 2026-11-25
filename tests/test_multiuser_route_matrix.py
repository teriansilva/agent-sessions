"""The multi-user route and producer matrices are checked against the code (#1469 P0, #2370).

`docs/multi-user/route-matrix.tsv` classifies every HTTP route and WebSocket the app registers
by the resource it touches, the permission it needs, who it authenticates and how often it runs.
`docs/multi-user/producer-matrix.tsv` does the same for what is not a route: the brokers,
notification producers, background effects and launch paths. Non-admin login stays closed while
any row in either file is ``unscoped`` (`docs/multi-user/contract.md`, "The first non-admin
admission gate"), so the files must never drift from the code:

* every registered route has exactly one row, and every row names a registered route;
* every enum column holds a known value, and each action belongs to its row's resource;
* a row's ``principal_source`` agrees with the auth dependency the route declares, and each
  WebSocket names the identity its handshake actually authenticates;
* every HQ operation (`ask_operations.CATALOG`) reaches a classified route;
* every producer row cites a symbol that exists.

When this fails because you added a route, the message prints a suggested row. Check it before
pasting it: the suggestion is a starting point, not a classification.
"""

from __future__ import annotations

import csv
import importlib
import inspect
import re
from pathlib import Path

import pytest
from starlette.routing import Mount, WebSocketRoute

from agent_sessions import ask_operations
from agent_sessions.main import create_app

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "docs" / "multi-user" / "route-matrix.tsv"
PRODUCERS = ROOT / "docs" / "multi-user" / "producer-matrix.tsv"

ROUTE_COLUMNS = [
    "method",
    "path",
    "module",
    "resource",
    "action",
    "principal_source",
    "effect",
    "frequency",
    "status",
    "note",
]
PRODUCER_COLUMNS = [
    "id",
    "kind",
    "symbol",
    "resource",
    "action",
    "principal_source",
    "frequency",
    "status",
    "note",
]

#: The permission catalog of #1469 ("Permissions and grants"), plus the resources a route can
#: touch that the catalog groups elsewhere: the Library, artifacts, a person's own data (`self`)
#: and the public routes that touch nothing (`none`).
ACTIONS: dict[str, frozenset[str]] = {
    "instance": frozenset(
        {
            "users.manage",
            "projects.create",
            "machines.register",
            "system.read",
            "system.configure",
            "audit.read_instance",
            "peers.manage",
        }
    ),
    "project": frozenset(
        {"view", "edit", "members.manage", "archive", "delete", "audit.read_project"}
    ),
    "session": frozenset(
        {
            "create",
            "start",
            "view",
            "suggest",
            "send",
            "comment",
            "input",
            "takeover",
            "stop",
            "edit_metadata",
            "archive",
            "delete",
            "share",
        }
    ),
    "mission": frozenset(
        {
            "create",
            "view",
            "edit",
            "comment",
            "start",
            "answer",
            "intervene",
            "stop",
            "approve_completion",
            "archive",
            "delete",
            "share",
        }
    ),
    "files": frozenset({"read", "write", "commit", "fetch", "push"}),
    "credentials": frozenset({"use", "manage"}),
    "library": frozenset({"view", "use", "manage"}),
    "artifact": frozenset({"view", "manage", "share"}),
    "self": frozenset({"read", "write"}),
    "none": frozenset({"public"}),
}
PRINCIPALS = frozenset({"login", "public-link", "view-token", "hmac", "peer", "pre-auth", "none"})
EFFECTS = frozenset({"read", "mutate", "stream", "background"})
FREQUENCIES = frozenset({"hot", "warm", "cold"})
STATUSES = frozenset({"unscoped", "admin-only", "scoped"})
PRODUCER_KINDS = frozenset({"broker", "notification", "background", "launch"})

#: What each WebSocket's handshake authenticates, read off its handler: the four login sockets
#: check the session cookie themselves (no FastAPI dependency), `/ws/fed` the peer's Noise key.
WS_PRINCIPALS = {
    "/ws/term/{sid}": "login",
    "/ws/voice/{call_id}": "login",
    "/ws/plugins/signin/{operation_id}": "login",
    "/ws/agent-accounts/{engine}/{account}/signin": "login",
    "/ws/fed": "peer",
}


def _read(path: Path, columns: list[str]) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        assert reader.fieldnames == columns, f"{path.name}: columns must be {columns}"
        rows = list(reader)
    for i, row in enumerate(rows, start=2):
        assert None not in row and all(
            v is not None for v in row.values()
        ), f"{path.name}:{i}: wrong number of fields"
    return rows


def _route_rows() -> dict[tuple[str, str], dict[str, str]]:
    out: dict[tuple[str, str], dict[str, str]] = {}
    for row in _read(ROUTES, ROUTE_COLUMNS):
        key = (row["method"], row["path"])
        assert key not in out, f"route-matrix.tsv: {key} has two rows"
        out[key] = row
    return out


def _deps(route) -> set[str]:
    found: set[str] = set()

    def walk(dependant) -> None:
        for sub in getattr(dependant, "dependencies", None) or []:
            found.add(getattr(sub.call, "__qualname__", "").split(".")[0])
            walk(sub)

    walk(getattr(route, "dependant", None))
    return found


def _app_routes(auth_cfg) -> dict[tuple[str, str], tuple[str, set[str]]]:
    """``(method, path) → (module, dependency factories)``; a WebSocket's method is ``WS``."""
    app = create_app(auth_cfg)
    found: dict[tuple[str, str], tuple[str, set[str]]] = {}
    for r in app.router.routes:
        path = getattr(r, "path", None)
        if path is None or isinstance(r, Mount):
            continue  # static file mounts serve the built SPA, not a capability
        module = getattr(getattr(r, "endpoint", None), "__module__", "").rsplit(".", 1)[-1]
        if isinstance(r, WebSocketRoute):
            found[("WS", path)] = (module, set())
            continue
        for method in (getattr(r, "methods", None) or set()) - {"HEAD"}:
            found[(method, path)] = (module, _deps(r))
    return found


def _principal(method: str, path: str, deps: set[str]) -> str | None:
    """The principal a route's declared dependency implies, or None when the handler checks."""
    if method == "WS":
        return WS_PRINCIPALS.get(path)
    if "require_session" in deps or "require_csrf_and_origin" in deps:
        return "login"
    if "require_share" in deps:
        return "public-link"
    return None


def _suggest(method: str, path: str, module: str, deps: set[str]) -> str:
    """A starting row for a route the matrix lacks. The mapping is deliberately coarse."""
    resource, verb = "instance", "system.configure"
    if "/sessions" in path or "/structured" in path or path.startswith("/ws/term"):
        resource, verb = "session", "view" if method == "GET" else "edit_metadata"
    elif "/missions" in path:
        resource, verb = "mission", "view" if method == "GET" else "edit"
    elif "/projects" in path:
        resource, verb = "project", "view" if method == "GET" else "edit"
    elif method == "GET":
        verb = "system.read"
    effect = "stream" if method == "WS" else ("read" if method == "GET" else "mutate")
    principal = _principal(method, path, deps) or "none"
    return "\t".join(
        [method, path, module, resource, verb, principal, effect, "warm", "unscoped", "TODO"]
    )


def test_every_registered_route_has_one_row_and_no_row_is_gone(auth_cfg):
    rows, live = _route_rows(), _app_routes(auth_cfg)
    missing = sorted(set(live) - set(rows))
    stale = sorted(set(rows) - set(live))
    hint = "\n".join(_suggest(m, p, *live[(m, p)]) for m, p in missing)
    assert not missing, (
        "routes route-matrix.tsv doesn't classify (suggested rows; check every column):\n" + hint
    )
    assert not stale, f"route-matrix.tsv rows for routes that no longer exist: {stale}"


def test_every_route_row_uses_known_values():
    for (method, path), row in _route_rows().items():
        where = f"{method} {path}"
        assert row["resource"] in ACTIONS, f"{where}: unknown resource {row['resource']!r}"
        assert (
            row["action"] in ACTIONS[row["resource"]]
        ), f"{where}: {row['action']!r} is not an action of {row['resource']!r}"
        assert row["principal_source"] in PRINCIPALS, f"{where}: unknown principal_source"
        assert row["effect"] in EFFECTS, f"{where}: unknown effect {row['effect']!r}"
        assert row["frequency"] in FREQUENCIES, f"{where}: unknown frequency"
        assert row["status"] in STATUSES, f"{where}: unknown status {row['status']!r}"
        assert row["note"].strip() and row["note"] != "TODO", f"{where}: note is required"


def test_principal_source_agrees_with_the_declared_dependency(auth_cfg):
    rows = _route_rows()
    for (method, path), (module, deps) in _app_routes(auth_cfg).items():
        row = rows.get((method, path))
        if row is None:
            continue  # the membership test reports it
        assert row["module"] == module, f"{method} {path}: module is {module!r}"
        implied = _principal(method, path, deps)
        if implied is not None:
            assert (
                row["principal_source"] == implied
            ), f"{method} {path}: its dependency implies {implied!r}"
        else:
            # The handler checks for itself, so a login it does not declare must be justified.
            assert row["principal_source"] != "login" or "checks" in row["note"], (
                f"{method} {path}: a login row without a declared dependency must say where it "
                "checks the session"
            )


def test_every_websocket_names_its_handshake_identity(auth_cfg):
    sockets = {p for (m, p) in _app_routes(auth_cfg) if m == "WS"}
    assert sockets == set(WS_PRINCIPALS), f"WebSockets changed: {sorted(sockets)}"
    for path, principal in WS_PRINCIPALS.items():
        assert _route_rows()[("WS", path)]["principal_source"] == principal
        assert _route_rows()[("WS", path)]["effect"] == "stream"


class _Anything(dict):
    """Placeholder fields/facts for asking an operation which route it calls."""

    def __missing__(self, key):
        return "x1"

    def get(self, key, default=None):
        return "x1"


def _template_regex(path: str) -> re.Pattern:
    parts = re.split(r"(\{[^}]+\})", path)
    body = "".join(
        ("[^/]+" if not p[1:-1].endswith(":path") else ".+") if p.startswith("{") else re.escape(p)
        for p in parts
    )
    return re.compile(f"^{body}$")


def _literal_calls(fn) -> list[tuple[str | None, str]]:
    """The route literals in a callable's source, each with the method literal that precedes it
    (``"POST", f"/api/..."``), or None when the source names none."""
    found = re.findall(
        # A `{…}` placeholder is taken whole, quotes inside it included (`{rec['id']}`).
        r"""(?:["'](GET|POST|PUT|PATCH|DELETE)["']\s*,\s*)?f?["'](/(?:api|ws)/(?:[^"'{?]|\{[^}]*\})*)""",
        inspect.getsource(fn),
    )
    return [(method or None, re.sub(r"\{[^}]*\}", "x1", p)) for method, p in found]


def _op_calls(op) -> list[tuple[str | None, str]]:
    """The ``(method, path)`` calls an HQ operation makes. ``route`` is asked with placeholder
    fields; a few read live state first (a project's folders) and refuse placeholders, so for
    those the paths are read off the route literals in their source. A multi-step operation
    (``run``) makes several calls, so its literals are checked as well."""
    calls: list[tuple[str | None, str]] = []
    try:
        method, path, _ = op.route("x1", _Anything(), _Anything())
        calls.append((method, path.split("?", 1)[0]))
    except Exception:  # noqa: BLE001 - any refusal falls back to the literals
        calls.extend(_literal_calls(op.route))
    if op.run is not None:
        calls.extend(_literal_calls(op.run))
    return calls


#: The SPA catch-all serves the app shell for ANY unknown GET path; it is not a capability, so
#: an HQ operation never counts as reaching it (Hermes, review 8920).
SPA_ROUTES = {("GET", "/"), ("GET", "/{spa_path:path}")}


def _capability_templates() -> list[tuple[str, re.Pattern]]:
    return [(m, _template_regex(p)) for (m, p) in _route_rows() if (m, p) not in SPA_ROUTES]


def _classified(method: str | None, path: str, templates) -> bool:
    """Whether a call reaches a classified capability route. A call whose method is unknown
    must still match a capability route by path; the SPA catch-all never counts."""
    return any(method in (None, m) and rx.match(path) for m, rx in templates)


def test_every_hq_operation_reaches_a_classified_route():
    templates = _capability_templates()
    for op in ask_operations.CATALOG.values():
        calls = _op_calls(op)
        assert calls, f"HQ operation {op.id}: no route call found"
        for method, path in calls:
            assert _classified(
                method, path, templates
            ), f"HQ operation {op.id} calls {method} {path}, which has no matrix row"


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/this-route-does-not-exist"),  # would match the SPA catch-all
        ("POST", "/api/this-route-does-not-exist"),
        (None, "/api/this-route-does-not-exist"),  # a literal fallback with no method
        ("POST", "/api/version"),  # a real path, the wrong method
    ],
)
def test_the_hq_check_refuses_unclassified_calls(method, path):
    assert not _classified(method, path, _capability_templates())


#: Routes that change who may do what. Their classification is pinned, so an enum-valid but
#: wrong row (editing a project vs creating one, Library admin vs disclosing a session) fails.
AUTHORITY_ROUTES = {
    ("POST", "/api/projects"): ("instance", "projects.create"),
    ("POST", "/api/shares"): ("session", "share"),
    ("DELETE", "/api/shares/{share_id}"): ("session", "share"),
    ("POST", "/api/shares/revoke-all"): ("session", "share"),
    ("POST", "/api/reply-shares"): ("session", "share"),
    ("DELETE", "/api/reply-shares/{share_id}"): ("session", "share"),
    ("POST", "/api/reply-shares/revoke-all"): ("session", "share"),
    ("POST", "/api/federation/grants"): ("session", "share"),
    ("PATCH", "/api/federation/grants/{grant_id}"): ("session", "share"),
    ("POST", "/api/federation/grants/{grant_id}/revoke"): ("session", "share"),
    ("POST", "/api/artifacts/{aid}/share"): ("artifact", "share"),
    ("DELETE", "/api/artifacts/{aid}/share"): ("artifact", "share"),
    ("POST", "/api/federation/invites"): ("instance", "peers.manage"),
    ("POST", "/api/federation/pairings/{pairing_id}/confirm"): ("instance", "peers.manage"),
    ("POST", "/api/federation/peers/{peer_id}/revoke"): ("instance", "peers.manage"),
    ("POST", "/api/webhooks"): ("library", "manage"),
    ("POST", "/api/structured/sessions/{key}/skip-permissions"): ("session", "send"),
    ("POST", "/api/password"): ("self", "write"),
}


def test_authority_changing_routes_keep_their_classification():
    rows = _route_rows()
    for key, want in AUTHORITY_ROUTES.items():
        assert key in rows, f"{key} is pinned here but no longer registered"
        got = (rows[key]["resource"], rows[key]["action"])
        assert got == want, f"{key[0]} {key[1]}: classified {got}, must be {want}"


def test_classification_is_consistent_with_the_method_and_path():
    for (method, path), row in _route_rows().items():
        where = f"{method} {path}"
        if method == "GET":
            assert row["effect"] != "mutate", f"{where}: a GET is never a mutation"
        elif method != "WS":
            assert row["effect"] != "read", f"{where}: a {method} is never a plain read"
        segments = path.strip("/").split("/")
        # A public-link or artifact-link change discloses or withdraws access: always `share`.
        if method not in ("GET", "WS") and (
            {"shares", "reply-shares"} & set(segments) or segments[-1] == "share"
        ):
            assert row["action"] == "share", f"{where}: changing a link is a share action"


def _resolve(symbol: str):
    module, _, qualname = symbol.partition(":")
    obj = importlib.import_module(f"agent_sessions.{module}")
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def test_every_producer_row_is_known_and_cites_a_real_symbol():
    seen: set[str] = set()
    for row in _read(PRODUCERS, PRODUCER_COLUMNS):
        where = f"producer {row['id']}"
        assert row["id"] not in seen, f"{where}: two rows"
        seen.add(row["id"])
        assert row["kind"] in PRODUCER_KINDS, f"{where}: unknown kind {row['kind']!r}"
        assert row["resource"] in ACTIONS, f"{where}: unknown resource"
        assert row["action"] in ACTIONS[row["resource"]], f"{where}: unknown action"
        assert row["principal_source"] in PRINCIPALS, f"{where}: unknown principal_source"
        assert row["frequency"] in FREQUENCIES, f"{where}: unknown frequency"
        assert row["status"] in STATUSES, f"{where}: unknown status"
        assert row["note"].strip(), f"{where}: note is required"
        try:
            _resolve(row["symbol"])
        except (ImportError, AttributeError) as exc:  # pragma: no cover - the failure message
            pytest.fail(f"{where}: {row['symbol']} does not resolve ({exc})")
