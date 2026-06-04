// VT scrollback sidecar (#271/#273 Path B). A unix-socket service that owns one headless-xterm
// emulator per session and answers snapshot requests with the reflowed-row payload. The Python app
// (webterm/scrollback) feeds it the PTY byte stream and, on attach, asks for snapshot(client cols).
//
// Wire protocol: newline-delimited JSON, one request per line, one response per line.
//   req:  {"id":N,"op":"feed|snapshot|reset|end|health|version","key":"<engine:id>","data":"<b64>","cols":C,"rows":R}
//   resp: {"id":N,"ok":true,"result":...}  |  {"id":N,"ok":false,"error":"..."}
// `data` (feed) is base64 of the raw PTY bytes. `result` (snapshot) is the replayable ANSI string.
//
// Single-threaded (Node event loop) → feed/snapshot for a key are processed in order, so the
// resize→read→restore in snapshot is atomic w.r.t. other requests.
import net from "node:net";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { EmulatorPool } from "./emulator.mjs";

export const VERSION = "1";

export function createServer(pool = new EmulatorPool()) {
  return net.createServer((sock) => {
    sock.setEncoding("utf8");
    let buf = "";
    sock.on("data", async (chunk) => {
      buf += chunk;
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        let req;
        try {
          req = JSON.parse(line);
        } catch {
          sock.write(JSON.stringify({ ok: false, error: "bad json" }) + "\n");
          continue;
        }
        const reply = (o) => sock.write(JSON.stringify({ id: req.id, ...o }) + "\n");
        try {
          switch (req.op) {
            case "open": // live mirror: create-or-resize the emulator to the agent's pty geometry
              pool.open(req.key, req.cols || 80, req.rows || 24);
              reply({ ok: true });
              break;
            case "feed": {
              const applied = await pool.feed(req.key, Buffer.from(req.data || "", "base64"));
              // Unknown session → not applied; tell the client so it marks the mirror dirty (#273).
              reply(applied ? { ok: true } : { ok: false, error: "no such session" });
              break;
            }
            case "snapshot": {
              const result = await pool.snapshot(req.key, req.cols || 80, req.rows || 40);
              reply({ ok: true, result });
              break;
            }
            case "rebuild": {
              const data = Buffer.from(req.data || "", "base64");
              const result = await pool.rebuild(req.key, data, req.cols || 80, req.rows || 40);
              reply({ ok: true, result });
              break;
            }
            case "reset":
              pool.reset(req.key);
              reply({ ok: true });
              break;
            case "end":
              pool.end(req.key);
              reply({ ok: true });
              break;
            case "health":
              reply({ ok: true, result: pool.stats() });
              break;
            case "version":
              reply({ ok: true, result: VERSION });
              break;
            default:
              reply({ ok: false, error: `unknown op: ${req.op}` });
          }
        } catch (e) {
          reply({ ok: false, error: String((e && e.message) || e) });
        }
      }
    });
    sock.on("error", () => {}); // a client hangup must not crash the sidecar
  });
}

// CLI entry: listen on $AGENT_SESSIONS_VT_SIDECAR_SOCK (or argv[2]). `import.meta.url` resolves
// symlinks but `process.argv[1]` does NOT, so when the installer launches us through its stable
// `current/...` symlink the naive `url === file://argv[1]` compare is FALSE and the bundled server
// exits without ever listening (#290 — flag-on prod silently fell back to transcript). Compare the
// REAL paths so it matches whether launched directly (tests, staging checkout) or via a symlink.
const isMain = (() => {
  try {
    return fs.realpathSync(fileURLToPath(import.meta.url)) === fs.realpathSync(process.argv[1]);
  } catch {
    return false;
  }
})();
if (isMain) {
  const sockPath = process.env.AGENT_SESSIONS_VT_SIDECAR_SOCK || process.argv[2];
  if (!sockPath) {
    console.error("usage: AGENT_SESSIONS_VT_SIDECAR_SOCK=/path/to.sock node server.mjs");
    process.exit(2);
  }
  try {
    fs.unlinkSync(sockPath);
  } catch {
    /* no stale socket */
  }
  const server = createServer();
  server.listen(sockPath, () => {
    try {
      fs.chmodSync(sockPath, 0o600); // owner-only
    } catch {
      /* best effort */
    }
    console.error(`vt-sidecar v${VERSION} listening on ${sockPath}`);
  });
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => {
      try {
        fs.unlinkSync(sockPath);
      } catch {
        /* ignore */
      }
      process.exit(0);
    });
  }
}
