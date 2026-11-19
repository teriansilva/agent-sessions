"""A fixture MCP server over stdio (#2163 tests). Plain stdlib, run as ``python -I <this file>``.

Its environment carries the settings under test. ``FIXTURE_ECHO=1`` makes it leak its
``API_KEY`` through every channel a check reads (stderr, serverInfo, a tool name, a probe result)
so the withholding can be asserted end to end. ``FIXTURE_ARGV_FILE`` (a plain setting) receives
its own ``/proc/self/cmdline``, so a test can prove no setting value rode argv.
"""

import json
import os
import sys

KEY = os.environ.get("API_KEY", "")
ECHO = os.environ.get("FIXTURE_ECHO") == "1"

argv_file = os.environ.get("FIXTURE_ARGV_FILE")
if argv_file:
    with open("/proc/self/cmdline", "rb") as f, open(argv_file, "wb") as out:
        out.write(f.read())
if ECHO:
    sys.stderr.write(f"starting with key {KEY}\n")
    sys.stderr.flush()

TOOLS = [
    {
        "name": "list_things",
        "description": "lists",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    },
    {"name": "write_thing", "description": "writes", "inputSchema": {"type": "object"}},
]
if ECHO:
    TOOLS.append({"name": f"leak_{KEY}", "inputSchema": {"type": "object"}})


def reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        version = f"1.0-{KEY}" if ECHO else "1.0"
        reply(
            rid,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture-stdio", "version": version},
            },
        )
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        if not KEY:
            reply(rid, {"content": [{"type": "text", "text": "401 no key"}], "isError": True})
        else:
            text = f"12 entries for {KEY}" if ECHO else "12 entries"
            reply(rid, {"content": [{"type": "text", "text": text}]})
    elif rid is not None:
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})
            + "\n"
        )
        sys.stdout.flush()
