"""Offline installed-CLI probe for #1903: every tool route a guarded `opencode-api` session has,
measured on the real `opencode acp`, under an operator configuration that allows everything.

Run with the repo venv (it imports `api_config_content`); no credentials, no inference, no
operator store: a throwaway HOME and a loopback scripted model. Requires
~/.opencode/bin/opencode. Writes `opencode_tool_routes.json` next to this file and prints what it
measured; `tests/test_mission_tool_routes.py` reads it. Re-run it on every opencode bump, and
whenever the guarded permission block changes (the test refuses a fixture measured under another
block).

What it does, in one `opencode acp` process per run:

* The operator's global config and the project's `opencode.json` both say ALLOW for everything
  (`*`, every built-in key, the fixture MCP server's tools), and the built-in `build` and
  `general` agents are declared all-allow too. The session is pinned to BattleLab's guarded agent
  (`api_config_content`), as the worker pins it.
* The scripted model reads the tool list opencode offers the guarded agent, then, one prompt per
  tool, calls exactly that tool with arguments that leave evidence when the tool really runs (a
  marker file, a file changed, a hit on the loopback server). Every permission request is
  answered `cancelled`, as BattleLab's worker answers one it can't place, so a tool that asked
  never runs. A tool that runs WITHOUT asking leaves its evidence: that is what the probe looks
  for.
* Sub-agents by both doors: the `task` tool, and slash commands that start a child session —
  opencode's built-in `/review` (`subtask: true`) and a project command declaring
  `subtask: true, agent: general` (#1892). The child's model calls `bash`.
"""

import hashlib
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
root = pathlib.Path(tempfile.mkdtemp(prefix="opencode-tool-routes-probe-"))
work = root / "work"
work.mkdir()
AGENT = "battlelab-api-0123456789abcdef"
OFFERED: list[str] = []  # the tools opencode offered the guarded agent, from the model request
SCHEMAS: dict[str, dict] = {}
HITS: list[str] = []  # paths the loopback server was asked for (webfetch evidence)
SCENARIO = ["none"]
CHILD_CALLS: dict[str, int] = {}  # model requests from a child session, per scenario
(root / "mcp.py").write_text("""import json, os, sys
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
        open(os.path.join(os.environ["PROBE_WORK"], "ran-mcp"), "w").write("ran")
        result = {"content": [{"type": "text", "text": "bug"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
""")


def _fill(schema: dict) -> dict:
    """Arguments for a tool nobody listed below: every required property, typed."""
    out = {}
    props = schema.get("properties") or {}
    for name in schema.get("required") or []:
        kind = (props.get(name) or {}).get("type")
        out[name] = {"array": [], "object": {}, "number": 1, "integer": 1, "boolean": False}.get(
            kind, "probe"
        )
    return out


def _arguments(tool: str) -> dict:
    """What the model calls `tool` with: arguments whose effect leaves evidence if it runs."""
    w = str(work)
    known = {
        "bash": {"command": f"printf ran > {w}/ran-bash", "description": "probe"},
        "edit": {"filePath": f"{w}/edit.txt", "oldString": "old", "newString": "new"},
        "multiedit": {
            "filePath": f"{w}/edit.txt",
            "edits": [{"oldString": "old", "newString": "multi"}],
        },
        "write": {"filePath": f"{w}/ran-write", "content": "ran"},
        "patch": {"patchText": "*** Begin Patch\n*** Add File: ran-patch\n+ran\n*** End Patch"},
        "apply_patch": {
            "patchText": "*** Begin Patch\n*** Add File: ran-patch\n+ran\n*** End Patch"
        },
        "read": {"filePath": f"{w}/edit.txt"},
        "grep": {"pattern": "old", "path": w},
        "glob": {"pattern": "*.txt", "path": w},
        "list": {"path": w},
        "webfetch": {"url": f"http://127.0.0.1:{server.server_port}/fetched", "format": "text"},
        "websearch": {"query": "probe"},
        "codesearch": {"query": "probe", "tokensNum": 1000},
        "todowrite": {
            "todos": [{"id": "1", "content": "probe", "status": "pending", "priority": "low"}]
        },
        "task": {
            "description": "Probe subagent",
            "prompt": "CHILD: run bash and report.",
            "subagent_type": "general",
        },
        "fixture_labels": {"repo": "agent-sessions"},
    }
    return known.get(tool) or _fill(SCHEMAS.get(tool) or {})


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

    def do_GET(self):
        HITS.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"fetched")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body.get("messages", [])
        users = [json.dumps(m.get("content")) for m in messages if m.get("role") == "user"]
        answered = bool(messages) and messages[-1].get("role") == "tool"
        tools = body.get("tools") or []
        wanted = re.search(r"CALL:([A-Za-z0-9_-]+)", users[-1]) if users else None
        if not tools:
            delta = {"content": "probe"}  # a title or summary request
        elif answered:
            delta = {"content": "done"}
        elif wanted:
            for t in tools:
                fn = t.get("function") or {}
                SCHEMAS[fn.get("name")] = fn.get("parameters") or {}
            if wanted.group(1) == "LIST":
                OFFERED[:] = sorted(SCHEMAS)
                delta = {"content": "listed"}
            else:
                delta = _call(wanted.group(1), _arguments(wanted.group(1)))
        else:
            # Not one of ours: a child session (a subagent or a subtask command's). It runs bash.
            CHILD_CALLS[SCENARIO[0]] = CHILD_CALLS.get(SCENARIO[0], 0) + 1
            marker = f"{work}/ran-child-{SCENARIO[0]}"
            delta = _call("bash", {"command": f"printf ran > {marker}", "description": "child"})
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

#: The hostile operator: every permission ALLOWED at the config level, in the project config, and
#: in the built-in agents' own blocks. None of it may reach BattleLab's guarded agent.
KEYS = (
    "bash edit read glob grep list task webfetch websearch codesearch skill todowrite todoread "
    "lsp question external_directory doom_loop plan_enter plan_exit"
).split()
ALLOW = {"*": "allow", **{k: "allow" for k in KEYS}, "fixture_*": "allow"}
PROVIDER = {
    "fixture": {
        "npm": "@ai-sdk/openai-compatible",
        "name": "fixture",
        "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "fixture"},
        "models": {"fixture": {"name": "fixture", "limit": {"context": 16000, "output": 2048}}},
    }
}
OPERATOR = {
    "model": "fixture/fixture",
    "small_model": "fixture/fixture",
    "enabled_providers": ["fixture"],
    "provider": PROVIDER,
    "permission": ALLOW,
    "agent": {"build": {"permission": ALLOW}, "general": {"permission": ALLOW}},
    "mcp": {"fixture": {"type": "local", "command": [sys.executable, str(root / "mcp.py")]}},
}
(root / ".config/opencode").mkdir(parents=True)
(root / ".config/opencode/opencode.json").write_text(json.dumps(OPERATOR))
(work / "opencode.json").write_text(json.dumps({"permission": ALLOW}))
(work / ".opencode/command").mkdir(parents=True)
(work / ".opencode/command/probe-sub.md").write_text(
    "---\ndescription: probe subtask\nagent: general\nsubtask: true\n---\nCHILD: run bash.\n"
)
(work / "edit.txt").write_text("old\n")
(work / ".opencode/skill/probe").mkdir(parents=True)
(work / ".opencode/skill/probe/SKILL.md").write_text(
    "---\nname: probe\ndescription: A probe skill.\n---\nRun bash.\n"
)
#: Permission keys opencode knows but did not offer the guarded agent here: calling one must not
#: run anything either.
UNOFFERED = ("task", "list", "question", "patch", "multiedit", "websearch", "codesearch", "lsp")


class ACP:
    def __init__(self, config: str):
        self.log = (root / "frames.jsonl").open("a")
        self.buf, self.serial = b"", 0
        self.err = (root / "stderr.log").open("ab")
        env = {
            "HOME": str(root),
            "PATH": os.environ["PATH"],
            "XDG_CONFIG_HOME": str(root / ".config"),
            "XDG_DATA_HOME": str(root / ".local/share"),
            "XDG_CACHE_HOME": str(root / ".cache"),
            "OPENCODE_SERVER_PASSWORD": "fixture-password-" + uuid.uuid4().hex,
            "OPENCODE_DISABLE_SHARE": "true",
            "OPENCODE_CONFIG_CONTENT": config,
            "PROBE_WORK": str(work),
        }
        self.proc = subprocess.Popen(
            [
                str(pathlib.Path.home() / ".opencode/bin/opencode"),
                *("acp", "--hostname", "127.0.0.1", "--port", "0", "--mdns=false"),
            ],
            cwd=work,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.err,
        )

    def send(self, frame):
        self.log.write(json.dumps({"dir": "out", "frame": frame}) + "\n")
        self.proc.stdin.write((json.dumps(frame) + "\n").encode())
        self.proc.stdin.flush()

    def call(self, method, params, timeout=60):
        """The request's result (None on timeout), every permission request seen meanwhile (each
        answered `cancelled`), and every tool call update."""
        self.serial += 1
        rid = "s" + str(self.serial)
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        asks, updates, end = [], [], time.monotonic() + timeout
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
            self.log.write(json.dumps({"dir": "in", "frame": m}) + "\n")
            if m.get("method") == "session/request_permission":
                asks.append(m["params"])
                answer = {"outcome": {"outcome": "cancelled"}}
                self.send({"jsonrpc": "2.0", "id": m["id"], "result": answer})
            elif m.get("method") == "session/update":
                update = m["params"].get("update", {})
                if update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
                    updates.append(update)
            elif "method" in m and "id" in m:
                # fs/* and terminal/*: BattleLab refuses every client service.
                error = {"code": -32601, "message": "Unsupported client method"}
                self.send({"jsonrpc": "2.0", "id": m["id"], "error": error})
            if m.get("id") == rid and "method" not in m:
                return m.get("result"), asks, updates
        return None, asks, updates

    def session(self):
        init = self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})[0]
        new = self.call("session/new", {"cwd": str(work), "mcpServers": []})[0]
        sid = new["sessionId"]
        self.call(
            "session/set_config_option", {"sessionId": sid, "configId": "mode", "value": AGENT}
        )
        return init, sid

    def prompt(self, sid, text, timeout=40):
        result, asks, updates = self.call(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
            timeout,
        )
        if result is None:
            # A notification in ACP: no id, no reply.
            frame = {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}
            self.send(frame)
        return result, asks, updates

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.log.close()
        self.err.close()


def _evidence(tool: str) -> bool:
    """Whether `tool` had its effect: the probe's own markers, a changed file, a server hit."""
    if tool in {"edit", "multiedit"}:
        return (work / "edit.txt").read_text() != "old\n"
    if tool == "webfetch":
        return "/fetched" in HITS
    if tool in {"patch", "apply_patch"}:
        return (work / "ran-patch").exists()
    if tool == "fixture_labels":
        return (work / "ran-mcp").exists()
    return (work / f"ran-{tool}").exists()


sys.path.insert(0, str(HERE.parent.parent / "src"))
from agent_sessions.engines.opencode import api_config_content  # noqa: E402

config = api_config_content(AGENT)
block = json.loads(config)["agent"][AGENT]["permission"]
report: dict = {
    "probe": "tests/fixtures/opencode_tool_routes_probe.py",
    "guarded_permission_sha256": hashlib.sha256(
        json.dumps(block, sort_keys=True).encode()
    ).hexdigest(),
    "operator": "allow everything: global config, project config, build and general agents",
    "tools": {},
    "commands": {},
}
cli = None
try:
    print("ROOT", root, flush=True)
    cli = ACP(config)
    init, sid = cli.session()
    report["version"] = (init.get("agentInfo") or {}).get("version")
    cli.prompt(sid, "CALL:LIST")
    report["offered"] = list(OFFERED)
    print("offered", OFFERED, flush=True)
    for tool in [*OFFERED, *(t for t in UNOFFERED if t not in OFFERED)]:
        result, asks, updates = cli.prompt(sid, f"CALL:{tool}")
        titles = sorted({u.get("title") for u in updates if isinstance(u.get("title"), str)})
        row = {
            "asked": len(asks),
            "ran": _evidence(tool),
            "stop": result and result.get("stopReason"),
            "statuses": sorted({u.get("status") for u in updates if u.get("status")}),
            "kinds": sorted({u.get("kind") for u in updates if u.get("kind")}),
            "invalid": "invalid" in titles,
            "offered": tool in OFFERED,
        }
        report["tools"][tool] = row
        print(tool, row, flush=True)
    cli.close()
    cli = None
    for name, text in (("review", "/review"), ("probe-sub", "/probe-sub")):
        # Each command in a process of its own: a hung prompt must not shadow the next one.
        SCENARIO[0] = name
        cli = ACP(config)
        _init, csid = cli.session()
        result, asks, updates = cli.prompt(csid, text, timeout=30)
        child = [u for u in updates if u.get("title") == "task" or u.get("kind") == "think"]
        row = {
            "asked": len(asks),
            "child_ran": (work / f"ran-child-{name}").exists(),
            "child_model_calls": CHILD_CALLS.get(name, 0),
            "returned": result is not None,
            "stop": result and result.get("stopReason"),
            "subagent_started": bool(child),
        }
        cli.close()
        cli = None
        report["commands"][text] = row
        print(text, row, flush=True)
finally:
    if cli is not None:
        cli.close()
    server.shutdown()
(HERE / "opencode_tool_routes.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
print(json.dumps(report, indent=2), flush=True)
