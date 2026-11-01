# ruff: noqa: E501, E402 — a fixture recorder: literal frames, and the repo import after setup
"""Record Codex app-server frames for #1726 against a local Responses-API mock. Throwaway HOME."""

import json
import os
import pathlib
import select
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
root = pathlib.Path(tempfile.mkdtemp(prefix="codex-1726-", dir="/tmp"))
home = root / "home"
work = root / "work"
codex_home = home / ".codex"
for p in (home, work, codex_home):
    p.mkdir(parents=True, exist_ok=True)
LOG = []


def sse(events):
    out = b""
    for e in events:
        out += f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
    return out


def done_usage(rid):
    return {
        "type": "response.completed",
        "response": {
            "id": rid,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    }


SERIAL = [0]


class API(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SERIAL[0] += 1
        rid = f"resp_{SERIAL[0]}"
        items = body.get("input", [])
        names = [t.get("name") or t.get("type") for t in body.get("tools", [])]
        LOG.append({"path": self.path, "tools": names, "last": items[-1] if items else None})
        last = items[-1] if items else {}
        user_texts = [
            c.get("text", "")
            for it in items
            if it.get("role") == "user"
            for c in (it.get("content") or [])
            if isinstance(c, dict)
        ]
        want = user_texts[-1] if user_texts else ""
        if (
            last.get("type") == "function_call_output"
            or last.get("type") == "custom_tool_call_output"
        ):
            evs = [
                {"type": "response.created", "response": {"id": rid}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": f"msg_{rid}",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
                done_usage(rid),
            ]
        elif "propose" in want:
            text = "Here is my plan.\n\n<proposed_plan>\n# Add the rail\n\n1. Codec events\n2. Snapshot fields\n</proposed_plan>\n"
            evs = [
                {"type": "response.created", "response": {"id": rid}},
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": f"msg_{rid}",
                        "content": [],
                    },
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": f"msg_{rid}",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                },
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": f"msg_{rid}",
                        "content": [{"type": "output_text", "text": text}],
                    },
                },
                done_usage(rid),
            ]
        elif "plan" in want:
            args = {
                "explanation": "Two steps, then verify.",
                "plan": [
                    {"step": "Read the codec", "status": "completed"},
                    {"step": "Add the plan event", "status": "in_progress"},
                    {"step": "Write the tests", "status": "pending"},
                ],
            }
            evs = [
                {"type": "response.created", "response": {"id": rid}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "id": f"fc_{rid}",
                        "call_id": f"call_{rid}",
                        "name": "update_plan",
                        "arguments": json.dumps(args),
                    },
                },
                done_usage(rid),
            ]
        elif "mcp" in want:
            tool = next((n for n in names if n and "labels" in n), None)
            ns = None
            for t in body.get("tools", []):
                if t.get("type") == "namespace" or "tools" in t:
                    for sub in t.get("tools", []):
                        if "labels" in (sub.get("name") or ""):
                            tool, ns = sub["name"], t.get("name")
            item = {
                "type": "function_call",
                "id": f"fc_{rid}",
                "call_id": f"call_{rid}",
                "name": tool or "mcp__fixture__labels",
                "arguments": json.dumps({"repo": "agent-sessions"}),
            }
            if ns:
                item["namespace"] = ns
            evs = [
                {"type": "response.created", "response": {"id": rid}},
                {"type": "response.output_item.done", "item": item},
                done_usage(rid),
            ]
        else:
            evs = [
                {"type": "response.created", "response": {"id": rid}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": f"msg_{rid}",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
                done_usage(rid),
            ]
        data = sse(evs)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = ThreadingHTTPServer(("127.0.0.1", 0), API)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
(codex_home / "config.toml").write_text(f"""model = "mock-model"
model_provider = "mock"
[model_providers.mock]
name = "mock"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
[tools.update_plan]
enabled = true
[mcp_servers.fixture]
command = "python3"
args = ["{HERE / 'mcp_fixture.py'}"]
[mcp_servers.broken]
command = "{root}/no-such-binary"
""")
sk = codex_home / "skills" / "release-notes"
sk.mkdir(parents=True)
(sk / "SKILL.md").write_text(
    "---\nname: release-notes\ndescription: Draft release notes from merged PRs.\n---\nWrite notes.\n"
)
env = {
    "HOME": str(home),
    "CODEX_HOME": str(codex_home),
    "PATH": "/usr/bin:/bin",
    "XDG_CONFIG_HOME": str(home / ".config"),
    "XDG_DATA_HOME": str(home / ".local/share"),
    "XDG_CACHE_HOME": str(home / ".cache"),
    "XDG_STATE_HOME": str(home / ".local/state"),
    "OPENAI_API_KEY": "sk-fixture",
    "TMPDIR": str(root),
}
proc = subprocess.Popen(
    [os.path.expanduser("~/.npm-global/bin/codex"), "app-server", "--listen", "stdio://"],
    cwd=work,
    env=env,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=open(root / "stderr.log", "wb"),
)
t0 = time.monotonic()
out = []


def rec(d, f):
    out.append({"t": round(time.monotonic() - t0, 3), "dir": d, "frame": f})


def send(f):
    rec("out", f)
    proc.stdin.write((json.dumps(f) + "\n").encode())
    proc.stdin.flush()


buf = b""


def pump(until=None, timeout=10.0):
    global buf
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r, _, _ = select.select([proc.stdout], [], [], 0.2)
        if r:
            chunk = os.read(proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                f = json.loads(line)
                rec("in", f)
                if until and until(f):
                    return f
    return None


EXP = os.environ.get("EXPERIMENTAL") == "1"
init = {"clientInfo": {"name": "battlelab_api", "title": "BattleLab", "version": "1"}}
if EXP:
    init["capabilities"] = {"experimentalApi": True}
send({"id": 1, "method": "initialize", "params": init})
pump(lambda f: f.get("id") == 1)
send({"method": "initialized"})
send(
    {
        "id": 2,
        "method": "thread/start",
        "params": {
            "cwd": str(work),
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "sandbox": "danger-full-access",
        },
    }
)
r = pump(lambda f: f.get("id") == 2)
tid = r["result"]["thread"]["id"]
pump(timeout=3)
send(
    {
        "id": 3,
        "method": "mcpServerStatus/list",
        "params": {"threadId": tid, "detail": "toolsAndAuthOnly", "limit": 1},
    }
)
r = pump(lambda f: f.get("id") == 3)
if r and r.get("result", {}).get("nextCursor"):
    send(
        {
            "id": 31,
            "method": "mcpServerStatus/list",
            "params": {
                "threadId": tid,
                "detail": "toolsAndAuthOnly",
                "limit": 1,
                "cursor": r["result"]["nextCursor"],
            },
        }
    )
    pump(lambda f: f.get("id") == 31)
send({"id": 4, "method": "skills/list", "params": {"cwds": [str(work)]}})
pump(lambda f: f.get("id") == 4)
TURNS = ((7, "propose it"),) if EXP else ((5, "make a plan"), (6, "call the mcp tool"))
for n, text in TURNS:
    params = {
        "threadId": tid,
        "input": [{"type": "text", "text": text, "text_elements": []}],
        "clientUserMessageId": f"op-{n}",
    }
    if EXP:
        params["collaborationMode"] = {"mode": "plan", "settings": {"model": "mock-model"}}
    send({"id": n, "method": "turn/start", "params": params})
    pump(lambda f: f.get("method") == "turn/completed", timeout=20)
proc.stdin.close()
proc.terminate()
proc.wait(10)
txt = json.dumps(out)
txt = txt.replace(str(root), "/tmp/fixture").replace(str(HERE), "/tmp/fixture/rec")
(root / "frames.jsonl").write_text("\n".join(json.dumps(x) for x in json.loads(txt)) + "\n")
(root / "mock.log").write_text(json.dumps(LOG, indent=1))
print(root)
