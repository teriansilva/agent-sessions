# ruff: noqa: E501, E402 — a fixture recorder: literal frames, and the repo import after setup
"""Record opencode ACP frames for #1726 against a local chat-completions mock. Throwaway HOME,
no operator login, no inference. Run with the repo venv (imports api_config_content)."""

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

HERE = pathlib.Path(__file__).resolve().parent
root = pathlib.Path(tempfile.mkdtemp(prefix="opencode-1726-", dir="/tmp"))
work = root / "work"
work.mkdir()
LOG = []
SCRIPT = {
    "todo": [
        (
            "todowrite",
            {
                "todos": [
                    {
                        "content": "Read the codec",
                        "status": "completed",
                        "priority": "high",
                        "id": "1",
                    },
                    {
                        "content": "Add the plan event",
                        "status": "in_progress",
                        "priority": "high",
                        "id": "2",
                    },
                    {
                        "content": "Write the tests",
                        "status": "pending",
                        "priority": "medium",
                        "id": "3",
                    },
                ]
            },
        )
    ],
    "skill": [("skill", {"name": "release-notes"})],
    "mcp": [("fixture_labels", {"repo": "agent-sessions"})],
}


class API(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = body.get("messages", [])
        names = [t["function"]["name"] for t in body.get("tools", [])]
        LOG.append({"tools": names, "n": len(msgs)})
        start = max(
            (
                i
                for i, m in enumerate(msgs)
                if m.get("role") == "user" and "please [" in json.dumps(m.get("content"))
            ),
            default=0,
        )
        op = json.dumps(msgs[start].get("content")) if msgs else ""
        calls = sum(1 for m in msgs[start:] if m.get("role") == "assistant" and m.get("tool_calls"))
        tool = None
        for key, seq in SCRIPT.items():
            if f"[{key}]" in op and calls < len(seq):
                tool = seq[calls]
        if tool and tool[0] not in names:
            tool = (next((n for n in names if tool[0].split("_")[-1] in n), tool[0]), tool[1])
        delta = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_" + uuid.uuid4().hex[:8],
                        "type": "function",
                        "function": {"name": tool[0], "arguments": json.dumps(tool[1])},
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
    "mcp": {
        "fixture": {"type": "local", "command": [sys.executable, str(HERE / "mcp_fixture.py")]},
        "remote-off": {"type": "remote", "url": "http://127.0.0.1:9/mcp", "enabled": False},
    },
}
(root / ".config/opencode/skill/release-notes").mkdir(parents=True)
(root / ".config/opencode/skill/release-notes/SKILL.md").write_text(
    "---\nname: release-notes\ndescription: Draft release notes from merged PRs.\n---\nWrite notes.\n"
)
(root / ".config/opencode/command").mkdir(parents=True)
(root / ".config/opencode/command/triage.md").write_text(
    "---\ndescription: Triage the open issues\n---\nTriage them.\n"
)
(root / ".config/opencode/opencode.json").write_text(json.dumps(config))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))
from agent_sessions.engines.opencode import api_config_content

agent = "battlelab-api-0123456789abcdef"
env = {
    "HOME": str(root),
    "PATH": os.environ["PATH"],
    "XDG_CONFIG_HOME": str(root / ".config"),
    "XDG_DATA_HOME": str(root / ".local/share"),
    "XDG_CACHE_HOME": str(root / ".cache"),
    "OPENCODE_SERVER_PASSWORD": "fixture-password-" + uuid.uuid4().hex,
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
    "OPENCODE_DISABLE_SHARE": "true",
    "OPENCODE_CONFIG_CONTENT": api_config_content(agent, bypass=True),
}
binary = str(pathlib.Path.home() / ".opencode/bin/opencode")
probe = subprocess.run(
    [binary, "debug", "config"], cwd=work, env=env, capture_output=True, timeout=60
)
(root / "debug-config.json").write_bytes(probe.stdout)
proc = subprocess.Popen(
    [binary, "acp", "--hostname", "127.0.0.1", "--port", "0", "--mdns=false"],
    cwd=work,
    env=env,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=open(root / "stderr.log", "wb"),
)
t0 = time.monotonic()
out = []
buf = b""


def rec(d, f):
    out.append({"t": round(time.monotonic() - t0, 3), "dir": d, "frame": f})


def send(f):
    rec("out", f)
    proc.stdin.write((json.dumps(f) + "\n").encode())
    proc.stdin.flush()


def pump(until=None, timeout=60):
    global buf
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if b"\n" not in buf:
            if not select.select([proc.stdout], [], [], 0.2)[0]:
                continue
            chunk = os.read(proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            buf += chunk
            continue
        line, buf = buf.split(b"\n", 1)
        try:
            m = json.loads(line)
        except ValueError:
            continue
        rec("in", m)
        if m.get("method") == "session/request_permission":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": m["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": "once"}},
                }
            )
        if until and until(m):
            return m
    return None


n = [0]


def call(method, params, timeout=60):
    n[0] += 1
    rid = f"battlelab-{n[0]}"
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    return pump(lambda m: m.get("id") == rid and "method" not in m, timeout)


call(
    "initialize",
    {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
    },
)
sid = call("session/new", {"cwd": str(work), "mcpServers": []})["result"]["sessionId"]
call("session/set_config_option", {"sessionId": sid, "configId": "mode", "value": agent})
pump(timeout=3)
for key in os.environ.get("KEYS", "todo skill mcp").split():
    call(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": f"please [{key}]"}]},
        timeout=90,
    )
pump(timeout=2)
proc.terminate()
proc.wait(10)
txt = (
    json.dumps(out)
    .replace(str(root), "/tmp/fixture")
    .replace(str(HERE), "/tmp/fixture/rec")
    .replace(env["OPENCODE_SERVER_PASSWORD"], "fixture-password")
)
(root / "frames.jsonl").write_text("\n".join(json.dumps(x) for x in json.loads(txt)) + "\n")
(root / "mock.log").write_text(json.dumps(LOG, indent=1))
print(root)
