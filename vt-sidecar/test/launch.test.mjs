// Regression for #290: the bundled server must START LISTENING whether it's launched directly OR
// through a symlink. The installer points the app at the sidecar via a stable `current/...` symlink;
// `import.meta.url` resolves that symlink but `process.argv[1]` does not, so the old
// `import.meta.url === file://argv[1]` guard was FALSE via a symlink and the server exited without
// listening → flag-on prod silently fell back to the transcript. We launch the real server.mjs both
// ways and assert each creates its socket.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SERVER = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "server.mjs");

function launch(entry, sock) {
  return spawn(process.execPath, [entry], {
    env: { ...process.env, AGENT_SESSIONS_VT_SIDECAR_SOCK: sock },
    stdio: "ignore",
  });
}

async function waitForSocket(sock, ms = 4000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (fs.existsSync(sock)) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

test("server listens whether launched directly OR via a symlink (the installer's current/...) (#290)", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vtlaunch-"));
  const procs = [];
  try {
    const directSock = path.join(dir, "direct.sock");
    procs.push(launch(SERVER, directSock));
    assert.ok(await waitForSocket(directSock), "listens when launched directly");

    // Reproduce the installer path: a symlink whose target is the real server.mjs.
    const link = path.join(dir, "server.mjs");
    fs.symlinkSync(SERVER, link);
    const linkSock = path.join(dir, "link.sock");
    procs.push(launch(link, linkSock));
    assert.ok(await waitForSocket(linkSock), "listens when launched via a symlink (#290)");
  } finally {
    for (const p of procs) p.kill("SIGTERM");
    await new Promise((r) => setTimeout(r, 150));
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
