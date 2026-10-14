"""Offline installed-CLI probe for #1339. Run with the repo venv; no credentials or inference.
A throwaway HOME and loopback scripted provider exercise native always/resume semantics.
The temporary directory is retained for inspection. Requires ~/.opencode/bin/opencode.
"""

import json
import os
import pathlib
import select
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = pathlib.Path(tempfile.mkdtemp(prefix="opencode-permissions-spike-"))
(root / "mcp.py").write_text("""import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m.get("method") == "initialize":
        result = {"protocolVersion": m["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture", "version": "1"}}
    elif m.get("method") == "tools/list":
        result = {"tools": [{"name": "labels", "description": "Fixture labels", "inputSchema": {
            "type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]}}]}
    else:
        result = {"content": [{"type": "text", "text": "bug"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
""")


class API(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body.get("messages", [])
        last = messages[-1] if messages else {}
        tool = last.get("role") == "user"
        mcp = tool and "MCP" in json.dumps(last)
        name = (
            next(
                (t["function"]["name"] for t in body.get("tools", []) if "labels" in str(t)),
                "fixture_labels",
            )
            if mcp
            else "bash"
        )
        delta = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_" + uuid.uuid4().hex[:8],
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                {"repo": "agent-sessions"}
                                if mcp
                                else {
                                    "command": "printf permission-spike",
                                    "description": "permission fixture",
                                }
                            ),
                        },
                    }
                ]
            }
            if tool
            else {"content": "done"}
        )
        chunks = [
            {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(("data: " + json.dumps(c) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


server = ThreadingHTTPServer(("127.0.0.1", 0), API)
threading.Thread(target=server.serve_forever, daemon=True).start()
config = {
    "model": "fixture/fixture",
    "small_model": "fixture/fixture",
    "enabled_providers": ["fixture"],
    "provider": {
        "fixture": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "fixture",
            "options": {
                "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                "apiKey": "fixture",
            },
            "models": {"fixture": {"name": "fixture", "limit": {"context": 16000, "output": 2048}}},
        }
    },
    "permission": {"*": "ask"},
    "mcp": {
        "fixture": {
            "type": "local",
            "command": [sys.executable, str(root / "mcp.py")],
            "environment": {"OPENCODE_SERVER_PASSWORD": ""},
        }
    },
}
(root / ".config/opencode").mkdir(parents=True)
(root / ".config/opencode/opencode.json").write_text(json.dumps(config))
serial = 0


class ACP:
    def __init__(self, bypass=False):
        self.err = (root / "stderr.log").open("ab")
        self.buf = b""
        env = {
            "HOME": str(root),
            "PATH": os.environ["PATH"],
            "XDG_CONFIG_HOME": str(root / ".config"),
            "XDG_DATA_HOME": str(root / ".local/share"),
            "XDG_CACHE_HOME": str(root / ".cache"),
            "OPENCODE_SERVER_PASSWORD": "fixture-password-" + uuid.uuid4().hex,
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_DISABLE_SHARE": "true",
        }
        from agent_sessions.engines.opencode import api_config_content

        self.agent = "battlelab-api-0123456789abcdef"
        env["OPENCODE_CONFIG_CONTENT"] = api_config_content(self.agent, bypass=bypass)
        self.proc = subprocess.Popen(
            [
                str(pathlib.Path.home() / ".opencode/bin/opencode"),
                "acp",
                "--hostname",
                "127.0.0.1",
                "--port",
                "0",
                "--mdns=false",
            ],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.err,
        )

    def send(self, frame):
        self.proc.stdin.write((json.dumps(frame) + "\n").encode())
        self.proc.stdin.flush()

    def call(self, method, params, answer="once"):
        global serial
        serial += 1
        rid = "s" + str(serial)
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        asks = []
        self.updates = []
        end = time.monotonic() + 60
        while time.monotonic() < end:
            if b"\n" not in self.buf:
                if not select.select([self.proc.stdout], [], [], 1)[0]:
                    continue
                chunk = os.read(self.proc.stdout.fileno(), 1048576)
                if not chunk:
                    raise RuntimeError("EOF " + (root / "stderr.log").read_text()[-1000:])
                self.buf += chunk
                continue
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("method") == "session/update":
                self.updates.append(m["params"]["update"])
            if m.get("method") == "session/request_permission":
                asks.append(m["params"])
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": m["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": answer}},
                    }
                )
            if m.get("id") == rid and "method" not in m:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result"), asks
        raise RuntimeError("timeout " + (root / "stderr.log").read_text()[-1400:])

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.err.close()


cli = ACP()
try:
    print("ROOT", root, flush=True)
    print(
        "initialize",
        cli.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})[0].get(
            "agentInfo"
        ),
        flush=True,
    )
    sid = cli.call("session/new", {"cwd": str(root), "mcpServers": []})[0]["sessionId"]
    cli.call(
        "session/set_config_option", {"sessionId": sid, "configId": "mode", "value": cli.agent}
    )
    for answer in ["always", "once"]:
        result, asks = cli.call(
            "session/prompt",
            {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": "Use bash to print permission-spike."}],
            },
            answer,
        )
        print("prompt", answer, "requests", len(asks), "stop", result.get("stopReason"), flush=True)
        assert len(asks) == (1 if answer == "always" else 0)
        if asks:
            print("request fixture", json.dumps(asks[0]), flush=True)
    result, asks = cli.call(
        "session/prompt",
        {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "Use the MCP labels tool."}],
        },
    )
    assert len(asks) == 1 and asks[0]["toolCall"]["rawInput"] == {}
    call = asks[0]["toolCall"]
    running = next(
        u
        for u in cli.updates
        if u.get("toolCallId") == call["toolCallId"] and u.get("status") == "in_progress"
    )
    assert running["rawInput"] == {"repo": "agent-sessions"}
    assert running["title"] == call["title"] and running["kind"] == call["kind"] == "other"
    print(
        "MCP metadata",
        call["rawInput"],
        "correlated running input",
        running["rawInput"],
        "stop",
        result.get("stopReason"),
        flush=True,
    )
    cli.close()
    cli = ACP()
    cli.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    cli.call("session/load", {"sessionId": sid, "cwd": str(root), "mcpServers": []})
    cli.call(
        "session/set_config_option", {"sessionId": sid, "configId": "mode", "value": cli.agent}
    )
    result, asks = cli.call(
        "session/prompt",
        {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "Use bash to print permission-spike."}],
        },
        "once",
    )
    print("resumed requests", len(asks), "stop", result.get("stopReason"), flush=True)
    assert len(asks) == 1
    cli.close()
    cli = ACP(bypass=True)
    cli.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    sid = cli.call("session/new", {"cwd": str(root), "mcpServers": []})[0]["sessionId"]
    cli.call(
        "session/set_config_option", {"sessionId": sid, "configId": "mode", "value": cli.agent}
    )
    result, asks = cli.call(
        "session/prompt",
        {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "Use bash to print permission-spike."}],
        },
        "once",
    )
    print("bypass requests", len(asks), "stop", result.get("stopReason"), flush=True)
    assert not asks
finally:
    cli.close()
    server.shutdown()
