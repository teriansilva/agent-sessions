# ruff: noqa: E501, E402 — a fixture recorder: literal frames, and the repo import after setup
"""Record Claude Code stream-json frames for #1726 against a local Messages-API mock. Throwaway HOME,
no operator login, no inference."""

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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))
from agent_sessions.native_protocol import ClaudeCodec  # the client's own argv

HERE = pathlib.Path(__file__).resolve().parent
root = pathlib.Path(tempfile.mkdtemp(prefix="claude-1726-", dir="/tmp"))
home = root / "home"
work = root / "work"
for p in (home, work):
    p.mkdir(parents=True, exist_ok=True)
LOG = []
SCRIPT = {
    "tasks": [
        (
            "TaskCreate",
            {
                "subject": "Read the codec",
                "description": "Find the dispatch",
                "activeForm": "Reading the codec",
            },
        ),
        (
            "TaskCreate",
            {
                "subject": "Add the plan event",
                "description": "Normalize it",
                "activeForm": "Adding the plan event",
            },
        ),
        ("TaskUpdate", {"taskId": "1", "status": "completed"}),
        ("TaskUpdate", {"taskId": "2", "status": "in_progress"}),
        ("TaskList", {}),
    ],
    "enterplan": [
        ("EnterPlanMode", {}),
        ("ExitPlanMode", {"plan": "# Add the rail\n\n1. Codec events\n2. Snapshot fields\n"}),
    ],
    "todo": (
        "TodoWrite",
        {
            "todos": [
                {
                    "content": "Read the codec",
                    "status": "completed",
                    "activeForm": "Reading the codec",
                },
                {
                    "content": "Add the plan event",
                    "status": "in_progress",
                    "activeForm": "Adding the plan event",
                },
                {
                    "content": "Write the tests",
                    "status": "pending",
                    "activeForm": "Writing the tests",
                },
            ]
        },
    ),
    "planmode": (
        "ExitPlanMode",
        {"plan": "# Add the rail\n\n1. Codec events\n2. Snapshot fields\n"},
    ),
    "skill": ("Skill", {"skill": "release-notes"}),
    "mcp": ("mcp__fixture__labels", {"repo": "agent-sessions"}),
}
SERIAL = [0]


def sse(events):
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


class API(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length") or 0) or 2) or b"{}"
        )
        SERIAL[0] += 1
        mid = f"msg_{SERIAL[0]:04d}"
        msgs = body.get("messages") or []
        LOG.append(
            {
                "path": self.path,
                "stream": body.get("stream"),
                "tools": [t.get("name") for t in body.get("tools") or []],
                "n": len(msgs),
            }
        )
        if not self.path.startswith("/v1/messages") or "count_tokens" in self.path:
            data = json.dumps({"input_tokens": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        tool = None
        # the newest plain operator text, and how many tool calls this turn made since
        start = max(
            (
                i
                for i, m in enumerate(msgs)
                if m.get("role") == "user"
                and (
                    isinstance(m.get("content"), str)
                    or any(
                        isinstance(b, dict)
                        and b.get("type") == "text"
                        and "please [" in b.get("text", "")
                        for b in m.get("content") or []
                    )
                )
            ),
            default=0,
        )
        op_text = msgs[start].get("content") if msgs else ""
        op_text = (
            op_text
            if isinstance(op_text, str)
            else " ".join(b.get("text", "") for b in op_text if isinstance(b, dict))
        )
        calls = sum(
            1
            for m in msgs[start:]
            if m.get("role") == "assistant"
            and any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in (m.get("content") or [])
            )
        )
        if body.get("tools"):
            for key, value in SCRIPT.items():
                if f"[{key}]" in op_text:
                    seq = value if isinstance(value, list) else [value]
                    tool = seq[calls] if calls < len(seq) else None
        model = body.get("model", "claude-fixture")
        if tool:
            name, args = tool
            block = {"type": "tool_use", "id": f"toolu_{SERIAL[0]:04d}", "name": name, "input": {}}
            stop = "tool_use"
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(args)}]
        else:
            block = {"type": "text", "text": ""}
            stop = "end_turn"
            deltas = [{"type": "text_delta", "text": "done"}]
        if not body.get("stream"):
            full = dict(block)
            if tool:
                full["input"] = tool[1]
            else:
                full["text"] = "done"
            data = json.dumps(
                {
                    "id": mid,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [full],
                    "stop_reason": stop,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        evs = [
            {
                "type": "message_start",
                "message": {
                    "id": mid,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": block},
        ]
        evs += [{"type": "content_block_delta", "index": 0, "delta": d} for d in deltas]
        evs += [
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
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
cdir = home / ".claude"
(cdir / "skills" / "release-notes").mkdir(parents=True)
(cdir / "skills" / "release-notes" / "SKILL.md").write_text(
    "---\nname: release-notes\ndescription: Draft release notes from merged PRs.\n---\nWrite notes.\n"
)
(cdir / "commands").mkdir()
(cdir / "commands" / "triage.md").write_text(
    "---\ndescription: Triage the open issues\n---\nTriage them.\n"
)
mcp = root / "mcp.json"
mcp.write_text(
    json.dumps(
        {
            "mcpServers": {
                "fixture": {"command": "python3", "args": [str(HERE / "mcp_fixture.py")]},
                "broken": {"command": str(root / "no-such-binary"), "args": []},
            }
        }
    )
)
env = {
    "HOME": str(home),
    "PATH": "/usr/bin:/bin:"
    + os.path.dirname(os.path.realpath(os.path.expanduser("~/.local/bin/claude"))),
    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
    "ANTHROPIC_API_KEY": "sk-ant-fixture",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "TMPDIR": str(root),
    "XDG_CONFIG_HOME": str(home / ".config"),
    "XDG_DATA_HOME": str(home / ".local/share"),
    "XDG_CACHE_HOME": str(home / ".cache"),
}
sid = str(uuid.uuid4())
argv = ClaudeCodec.argv(
    os.path.realpath(os.path.expanduser("~/.local/bin/claude")),
    sid,
    model="claude-sonnet-4-5",
    mcp_config=str(mcp),
)
proc = subprocess.Popen(
    argv,
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


def pump(until=None, timeout=30.0):
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
                if (
                    f.get("type") == "control_request"
                    and f["request"].get("subtype") == "can_use_tool"
                ):
                    send(
                        {
                            "type": "control_response",
                            "response": {
                                "subtype": "success",
                                "request_id": f["request_id"],
                                "response": {
                                    "behavior": "allow",
                                    "updatedInput": f["request"].get("input", {}),
                                },
                            },
                        }
                    )
                if until and until(f):
                    return f
    return None


send(
    {
        "type": "control_request",
        "request_id": "battlelab-1",
        "request": {"subtype": "initialize", "hooks": None},
    }
)
pump(lambda f: f.get("type") == "control_response")
for key in os.environ.get("KEYS", "todo planmode skill mcp").split():
    op = str(uuid.uuid4())
    send(
        {
            "type": "user",
            "uuid": op,
            "session_id": sid,
            "message": {"role": "user", "content": f"please [{key}]"},
            "parent_tool_use_id": None,
            "origin": {"kind": "human"},
            "client_composed": True,
        }
    )
    pump(lambda f: f.get("type") == "result", timeout=60)
proc.stdin.close()
try:
    proc.wait(10)
except subprocess.TimeoutExpired:
    proc.terminate()
    proc.wait(10)
txt = json.dumps(out).replace(str(root), "/tmp/fixture").replace(str(HERE), "/tmp/fixture/rec")
(root / "frames.jsonl").write_text("\n".join(json.dumps(x) for x in json.loads(txt)) + "\n")
(root / "mock.log").write_text(json.dumps(LOG, indent=1))
print(root)
