"""Offline installed-CLI probe for #1891: what `opencode acp` tells an ACP client about a `task`
subagent, and whether a subagent's permission request ever reaches it.

Run with the repo venv (it imports `api_config_content`); no credentials, no inference, no operator
store: a throwaway HOME and a loopback scripted model. Requires ~/.opencode/bin/opencode. Writes
`opencode_subagent_{ask,bypass,guarded}.jsonl` next to this file and prints what it measured; see
`opencode_subagent.PROVENANCE.md`. Re-run it on every opencode bump before relaxing `task: deny`.

The scripted model: in the PARENT session it calls `task` (a `general` subagent) and then answers
`done`; in the CHILD session (its first user message is the task prompt, `CHILD: …`) it calls
`bash` and then answers `child-done`.
"""

import json
import os
import pathlib
import re
import select
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
root = pathlib.Path(tempfile.mkdtemp(prefix="opencode-subagent-probe-"))
AGENT = "battlelab-api-0123456789abcdef"
CHILD_TOOL = ["bash"]  # what the child calls: `bash`, or the fixture MCP server's `labels`
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


def _call(name, arguments):
    return {
        "tool_calls": [
            {
                "index": 0,
                "id": "call_" + uuid.uuid4().hex[:8],
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    }


class API(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body.get("messages", [])
        first_user = next((m for m in messages if m.get("role") == "user"), {})
        child = "CHILD:" in json.dumps(first_user.get("content"))
        answered = bool(messages) and messages[-1].get("role") == "tool"
        if not body.get("tools"):
            delta = {"content": "probe"}  # a title or summary request
        elif child:
            mcp = next(
                (t["function"]["name"] for t in body["tools"] if "labels" in str(t)),
                "fixture_labels",
            )
            delta = (
                {"content": "child-done"}
                if answered
                else _call("bash", {"command": "printf child-ran", "description": "probe"})
                if CHILD_TOOL[0] == "bash"
                else _call(mcp, {"repo": "agent-sessions"})
            )
        else:
            delta = (
                {"content": "done"}
                if answered
                else _call(
                    "task",
                    {
                        "description": "Probe subagent",
                        "prompt": "CHILD: run printf child-ran with bash and report it.",
                        "subagent_type": "general",
                    },
                )
            )
        done = "tool_calls" if "tool_calls" in delta else "stop"
        chunks = [
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": done}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            c.update(id="fixture", object="chat.completion.chunk", created=1, model="fixture")
            self.wfile.write(("data: " + json.dumps(c) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


server = ThreadingHTTPServer(("127.0.0.1", 0), API)
threading.Thread(target=server.serve_forever, daemon=True).start()
(root / ".config/opencode").mkdir(parents=True)
(root / ".config/opencode/opencode.json").write_text(
    json.dumps(
        {
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
                    "models": {
                        "fixture": {"name": "fixture", "limit": {"context": 16000, "output": 2048}}
                    },
                }
            },
        }
    )
)


def _sanitize(value):
    text = json.dumps(value).replace(str(root), "/tmp/fixture")
    return json.loads(re.sub(r"ses_[A-Za-z0-9]+", lambda m: "ses_fixture" + m.group(0)[-6:], text))


class ACP:
    def __init__(self, config: str, fixture: str):
        self.out = (HERE / fixture).open("w")
        self.t0, self.buf, self.serial = time.monotonic(), b"", 0
        self.err = (root / "stderr.log").open("ab")
        env = {
            "HOME": str(root),
            "PATH": os.environ["PATH"],
            "XDG_CONFIG_HOME": str(root / ".config"),
            "XDG_DATA_HOME": str(root / ".local/share"),
            "XDG_CACHE_HOME": str(root / ".cache"),
            "OPENCODE_SERVER_PASSWORD": "fixture-password-" + uuid.uuid4().hex,
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_DISABLE_SHARE": "true",
            "OPENCODE_CONFIG_CONTENT": config,
        }
        self.proc = subprocess.Popen(
            [
                str(pathlib.Path.home() / ".opencode/bin/opencode"),
                *("acp", "--hostname", "127.0.0.1", "--port", "0", "--mdns=false"),
            ],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.err,
        )

    def log(self, direction, frame):
        t = round(time.monotonic() - self.t0, 3)
        self.out.write(json.dumps({"t": t, "dir": direction, "frame": _sanitize(frame)}) + "\n")
        self.out.flush()

    def send(self, frame):
        self.log("out", frame)
        self.proc.stdin.write((json.dumps(frame) + "\n").encode())
        self.proc.stdin.flush()

    def call(self, method, params, timeout=60):
        """The request's result (None on timeout) and every permission request seen meanwhile,
        each answered `cancelled`, as BattleLab's worker answers one it can't place."""
        self.serial += 1
        rid = "s" + str(self.serial)
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        asks, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            if b"\n" not in self.buf:
                if select.select([self.proc.stdout], [], [], 0.5)[0]:
                    chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
                    if not chunk:
                        raise RuntimeError("EOF " + (root / "stderr.log").read_text()[-1000:])
                    self.buf += chunk
                continue
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                m = json.loads(line)
            except ValueError:
                continue
            self.log("in", m)
            if m.get("method") == "session/request_permission":
                asks.append(m["params"])
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": m["id"],
                        "result": {"outcome": {"outcome": "cancelled"}},
                    }
                )
            if m.get("id") == rid and "method" not in m:
                return m.get("result"), asks
        return None, asks

    def session(self):
        self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        sid = self.call("session/new", {"cwd": str(root), "mcpServers": []})[0]["sessionId"]
        self.call(
            "session/set_config_option", {"sessionId": sid, "configId": "mode", "value": AGENT}
        )
        return sid

    def prompt(self, sid, timeout):
        text = "PARENT: run a general subagent."
        return self.call(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
            timeout,
        )

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.out.close()
        self.err.close()


def task_updates(fixture):
    out = []
    for line in (HERE / fixture).read_text().splitlines():
        update = json.loads(line)["frame"].get("params", {}).get("update", {})
        if update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
            out.append(update)
    return out


sys.path.insert(0, str(HERE.parent.parent / "src"))
from agent_sessions.engines.opencode import api_config_content  # noqa: E402

cli = None
try:
    print("ROOT", root, flush=True)
    # 1. A subagent that asks (the config-level block of main before #1891): is the ask sent?
    asking = json.loads(api_config_content(AGENT, bypass=True))
    asking["permission"] = {"*": "ask"}
    cli = ACP(json.dumps(asking), "opencode_subagent_ask.jsonl")
    sid = cli.session()
    result, asks = cli.prompt(sid, timeout=30)
    print("ask: prompt result", result, "permission requests", len(asks), flush=True)
    assert result is None and asks == [], "a child's ask now reaches the client: re-open #1891"
    cli.call("session/cancel", {"sessionId": sid}, timeout=10)
    cli.close()
    # 2. Bypass as shipped by #1891: the subagent runs unprompted.
    cli = ACP(api_config_content(AGENT, bypass=True), "opencode_subagent_bypass.jsonl")
    sid = cli.session()
    result, asks = cli.prompt(sid, timeout=60)
    updates = task_updates("opencode_subagent_bypass.jsonl")
    print("bypass:", result and result.get("stopReason"), "asks", len(asks), flush=True)
    assert result and result["stopReason"] == "end_turn" and asks == []
    assert any(u.get("status") == "completed" and "child-done" in json.dumps(u) for u in updates)
    cli.close()
    # 3. Guarded as shipped by #1891: `task` is refused at once, and nothing hangs.
    cli = ACP(api_config_content(AGENT), "opencode_subagent_guarded.jsonl")
    sid = cli.session()
    result, asks = cli.prompt(sid, timeout=60)
    updates = task_updates("opencode_subagent_guarded.jsonl")
    print("guarded:", result and result.get("stopReason"), "asks", len(asks), flush=True)
    print("guarded task updates", json.dumps(updates)[:1500], flush=True)
    assert result and result["stopReason"] == "end_turn" and asks == []
    # 4. Bypass under an operator config-level block with a key ours doesn't name (review on
    # #1891): opencode keeps the operator's key order, so `fixture_*: ask` follows our `*`, and a
    # subagent's call to that MCP server asks, unless the worker restates the operator's keys.
    # (A key ours names, `bash` say, is no hole: a new key is appended AFTER the operator's.)
    CHILD_TOOL[0] = "mcp"
    global_config = root / ".config/opencode/opencode.json"
    operator = {"*": "ask", "fixture_*": "ask"}
    fixture_mcp = {"type": "local", "command": [sys.executable, str(root / "mcp.py")]}
    global_config.write_text(
        json.dumps(
            {
                **json.loads(global_config.read_text()),
                "permission": operator,
                "mcp": {"fixture": fixture_mcp},
            }
        )
    )
    cli = ACP(api_config_content(AGENT, bypass=True), "opencode_subagent_operator_unrestated.jsonl")
    sid = cli.session()
    result, asks = cli.prompt(sid, timeout=30)
    print("operator block, not restated:", result, "asks", len(asks), flush=True)
    assert result is None and asks == [], "the operator's key no longer wins: re-check #1891"
    cli.call("session/cancel", {"sessionId": sid}, timeout=10)
    cli.close()
    restated = api_config_content(AGENT, bypass=True, operator_permission=operator)
    cli = ACP(restated, "opencode_subagent_operator_restated.jsonl")
    sid = cli.session()
    result, asks = cli.prompt(sid, timeout=60)
    updates = task_updates("opencode_subagent_operator_restated.jsonl")
    print("operator block, restated:", result and result.get("stopReason"), flush=True)
    assert result and result["stopReason"] == "end_turn" and asks == []
    assert any(u.get("status") == "completed" and "child-done" in json.dumps(u) for u in updates)
finally:
    if cli is not None:
        cli.close()
    server.shutdown()
