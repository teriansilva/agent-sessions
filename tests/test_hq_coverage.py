"""HQ's coverage ledger is checked against the code, never against its own table (#1558, #1559).

`docs/hq-coverage.md` maps every operator capability to how HQ reaches it. This test derives the
capabilities from three independent sources and fails on any the ledger doesn't map:

* every route the app registers (method + path, from ``app.router``; WebSockets as ``WS``);
* every field ``POST /api/prefs`` accepts — the top-level keys read off the handler's own checks,
  and the nested keys of the blocks whose ledger rows split by field, read off their validators'
  defaults — because one route carries dozens of separate settings;
* every ``/api/…`` and ``/ws/…`` literal in ``web/src``, which must name a mapped route.

It also fails on a ledger entry that no longer exists, so the file can't drift the other way.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from starlette.routing import Mount, WebSocketRoute

from agent_sessions import prefs
from agent_sessions.main import create_app

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "hq-coverage.md"
CODES = {"have", "#1554", "#1443", "#1786", "never", "device", "n/a", "prefs", "nested"} | {
    f"P{i}" for i in range(1, 8)
}
#: The prefs blocks whose ledger rows split by field, and where their fields are defined.
NESTED = {
    "ai_review": lambda: set(prefs._AI_REVIEW_DEFAULTS),
    "orchestrator": lambda: set(prefs._ORCH_DEFAULTS),
    "agent_defaults": lambda: set(prefs.AGENT_DEFAULTS_KEYS),
    "forge": lambda: set(prefs._FORGE_DEFAULTS),
}


def _block(name: str) -> list[tuple[str, str]]:
    text = DOC.read_text()
    m = re.search(rf"```{name}\n(.*?)\n```", text, re.S)
    assert m, f"docs/hq-coverage.md has no {name} block"
    rows = []
    for line in m.group(1).splitlines():
        # A comment is two spaces, `#` and a space; a code like `#1554` has no space after it.
        body = re.split(r"\s{2,}# ", line, maxsplit=1)[0].strip()
        if body:
            rows.append(tuple(body.rsplit(None, 1)))
    return rows


def _route_map() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for head, code in _block("hq-routes"):
        method, path = head.split(None, 1)
        assert (method, path) not in out, f"{method} {path} is mapped twice"
        out[(method, path)] = code
    return out


def _app_routes(auth_cfg) -> set[tuple[str, str]]:
    app = create_app(auth_cfg)
    found: set[tuple[str, str]] = set()
    for r in app.router.routes:
        path = getattr(r, "path", None)
        if path is None or isinstance(r, Mount):
            continue  # static files are not capabilities
        if isinstance(r, WebSocketRoute):
            found.add(("WS", path))
            continue
        methods = getattr(r, "methods", None) or set()
        found |= {(m, path) for m in methods if m != "HEAD"}
    return found


def test_every_registered_route_is_mapped_and_no_mapped_route_is_gone(auth_cfg):
    mapped, live = _route_map(), _app_routes(auth_cfg)
    missing = sorted(live - set(mapped))
    stale = sorted(set(mapped) - live)
    assert not missing, f"routes the HQ ledger doesn't map: {missing}"
    assert not stale, f"ledger entries for routes that no longer exist: {stale}"


def test_every_code_is_a_known_code():
    for (method, path), code in _route_map().items():
        assert code in CODES, f"{method} {path}: unknown code {code!r}"
    for key, code in _block("hq-prefs"):
        assert code in CODES, f"prefs {key}: unknown code {code!r}"


def _prefs_handler_keys() -> set[str]:
    tree = ast.parse((ROOT / "src/agent_sessions/routes/system.py").read_text())
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "set_prefs"
    )
    keys: set[str] = set()
    for node in ast.walk(fn):
        # `"<key>" in payload`
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and any(isinstance(o, ast.In) for o in node.ops)
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id == "payload"
        ):
            keys.add(node.left.value)
        # `for key, setter in (("<key>", setter), …)`
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
            for elt in node.iter.elts:
                if (
                    isinstance(elt, ast.Tuple)
                    and elt.elts
                    and isinstance(elt.elts[0], ast.Constant)
                    and isinstance(elt.elts[0].value, str)
                ):
                    keys.add(elt.elts[0].value)
    return keys


def test_every_prefs_field_is_mapped_including_the_split_blocks():
    mapped = dict(_block("hq-prefs"))
    handler = _prefs_handler_keys()
    assert len(handler) > 20, "the prefs handler's keys were not found: has the handler moved?"
    expected = set(handler)
    for block, fields in NESTED.items():
        assert block in handler, f"{block} is no longer a prefs block"
        expected |= {f"{block}.{f}" for f in fields()}
    missing = sorted(expected - set(mapped))
    stale = sorted(set(mapped) - expected)
    assert not missing, f"prefs fields the HQ ledger doesn't map: {missing}"
    assert not stale, f"ledger entries for prefs fields that no longer exist: {stale}"
    for block in NESTED:
        assert mapped[block] == "nested", f"{block} is split by field: map it as nested"


def _route_regexes() -> list[re.Pattern]:
    out = []
    for _method, path in _route_map():
        rx = re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(path))
        rx = rx.replace("[^/]+", "(?:[^/]+|\\{\\})")
        out.append(re.compile(f"^{rx}$"))
    return out


def test_every_api_literal_in_the_ui_names_a_mapped_route():
    patterns = _route_regexes()
    unknown = []
    for f in (ROOT / "web" / "src").rglob("*.ts*"):
        if ".test." in f.name or "/test/" in str(f) or f.name == "sw-denylist.ts":
            continue
        for m in re.finditer(r"[`\"'](/(?:api|ws)/[^`\"'\s]*)[`\"']", f.read_text()):
            raw = m.group(1)
            # Prose in comments and type docs ("/api/git/*", "/api/config.x"), not call sites.
            if any(c in raw for c in "*[<….") or raw.endswith("/"):
                continue
            path = re.sub(r"\$\{[^}]*\}", "{}", raw).split("?")[0]
            # A base the UI builds paths from (`/api/public`, `/ws/term`) prefixes mapped routes.
            prefix = any(mp.startswith(path + "/") for _m, mp in _route_map())
            if not prefix and not any(p.match(path) for p in patterns):
                unknown.append(f"{f.relative_to(ROOT)}: {raw}")
    assert not unknown, f"UI call sites naming no mapped route: {unknown}"
