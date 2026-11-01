# ruff: noqa: E501, E402 — a fixture recorder: literal frames, and the repo import after setup
"""A one-tool stdio MCP server for the #1726 recorders (`labels`), answering every call with `bug`."""

import json
import sys

for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m.get("method") == "initialize":
        result = {
            "protocolVersion": m["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif m.get("method") == "tools/list":
        result = {
            "tools": [
                {
                    "name": "labels",
                    "description": "Fixture labels",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"repo": {"type": "string"}},
                        "required": ["repo"],
                    },
                }
            ]
        }
    else:
        result = {"content": [{"type": "text", "text": "bug"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
