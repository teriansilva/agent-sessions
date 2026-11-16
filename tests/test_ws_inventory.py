"""The outbound WebSocket inventory (#1246 P1).

`tests/test_prompts_registry.py` pins every outbound HTTP call to a module, a function and a
count. A WebSocket client was outside it: `websockets` is not an HTTP client library there, and a
socket's `.send()` is deliberately not counted as a request. So a new module could open a
WebSocket to anywhere without any list changing.

For a WebSocket the outbound act is the **connect**, so that is what is pinned here, on the same
two layers:

1. only the listed modules may import ``websockets`` at all;
2. inside them, every place that names a connect function (``websockets.connect``,
   ``websockets.unix_connect``, or a local subclass of one) is pinned to its function and a count.
   A *reference* counts like a call: passing ``websockets.connect`` to something else to call is
   still this module opening a socket.

Both directions fail: a new site, and an entry that no longer describes the code.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_sessions"
LIB = "websockets"
CONNECTORS = frozenset({"connect", "unix_connect"})

#: module (path under the package, no suffix) → why it may open a WebSocket.
WS_MODULES = {
    "homefree/agent": "the Home Free agent's two legs to the relay the operator configured",
    "homefree/appproxy": "Home Free's proxy to this box's OWN app on loopback",
    "routes/voice": "the call worker's unix socket on this host",
    "federation/service": "the peer dialer: vetted address, no redirect, Noise inside (#1246)",
}

#: (module, function) → how many places name a connect function there.
WS_CONNECT_SITES = {
    ("homefree/agent", "run_once"): 1,
    ("homefree/agent", "_handle_session"): 1,
    ("homefree/appproxy", "__init__"): 1,  # the default of an injectable connect, not a call
    ("routes/voice", "call_socket"): 1,
    # The subclass that refuses redirects, and the one call that uses it.
    ("federation/service", "<module>"): 1,
    ("federation/service", "dial_direct"): 1,
}


def _imports_lib(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == LIB or a.name.startswith(LIB + ".") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == LIB or node.module.startswith(LIB + "."):
                return True
    return False


def _is_connector(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr in CONNECTORS
        and isinstance(node.value, ast.Name)
        and node.value.id == LIB
    )


def _imported_connectors(tree: ast.Module) -> set[str]:
    """Names bound by ``from websockets… import connect`` (under any alias)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == LIB or node.module.startswith(LIB + "."):
                for alias in node.names:
                    if alias.name in CONNECTORS:
                        names.add(alias.asname or alias.name)
    return names


def connect_sites(tree: ast.Module) -> dict[str, int]:
    """function name → how many times a connect function is named inside it."""
    found: dict[str, int] = {}
    bare = _imported_connectors(tree)
    # A local subclass of a connect function IS one; calling it opens a socket.
    local = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(_is_connector(b) or (isinstance(b, ast.Name) and b.id in bare) for b in node.bases)
    }

    def visit(node: ast.AST, owner: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = node.name
        if _is_connector(node) or (isinstance(node, ast.Name) and node.id in bare):
            found[owner] = found.get(owner, 0) + 1
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in local
        ):
            found[owner] = found.get(owner, 0) + 1
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, "<module>")
    return found


def _module(path: Path) -> str:
    return path.relative_to(SRC).with_suffix("").as_posix()


def test_the_websocket_inventory_matches_the_code():
    importers: set[str] = set()
    found: dict[tuple[str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        if _imports_lib(tree):
            importers.add(_module(path))
        for func, count in connect_sites(tree).items():
            found[(_module(path), func)] = count
    assert importers == set(WS_MODULES)
    assert found == WS_CONNECT_SITES


def test_the_scanner_sees_every_way_to_name_a_connect():
    planted = ast.parse(
        "import websockets\n"
        "from websockets.asyncio.client import connect as dial\n"
        "class Mine(websockets.connect):\n"
        "    pass\n"
        "async def a():\n"
        "    await websockets.connect('ws://x')\n"
        "async def b():\n"
        "    await websockets.unix_connect('/s')\n"
        "def c(opener=websockets.connect):\n"
        "    return opener\n"
        "async def d():\n"
        "    await dial('ws://x')\n"
        "async def e():\n"
        "    await Mine('ws://x')\n"
        "async def not_one(ws):\n"
        "    await ws.send(b'x')\n"
        "    return connect_sites\n"
    )
    assert _imports_lib(planted)
    assert connect_sites(planted) == {"<module>": 1, "a": 1, "b": 1, "c": 1, "d": 1, "e": 1}


def test_a_module_without_the_library_is_not_an_importer():
    assert not _imports_lib(ast.parse("import asyncio\nfrom urllib.parse import urlsplit\n"))
