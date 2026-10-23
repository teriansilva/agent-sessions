import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { createHmac } from "node:crypto";
import { createServer } from "node:net";
import { writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { killGroupsAndWait, removeTree, withTempHome, STARTUP_BUDGET_MS } from "./harness";

/** The REAL app serving the pages outside the SPA (#1596): sign-in, the TOTP step, change
 *  password and device-link approve, rendered by the server from its templates with password auth
 *  on — nothing mocked at the network edge. Each test gets its own throwaway HOME (never the real
 *  ~/.claude), its own port and its own process group, killed by PID group, never by name.
 *
 *  Runs where the Python half exists (`uv`): the `appmode-e2e` workflow, and locally. web-ci is
 *  Node-only, so specs using this SKIP there (see `PY_STACK_AVAILABLE` in appmode.ts). */

const REPO_ROOT = resolve(process.cwd(), "..");
export const USER = "commander";
export const PASSWORD = "correct horse battery";

export interface App {
  base: string;
  home: string;
  logs: () => string;
  stop: () => Promise<void>;
}

function freePort(): Promise<number> {
  return new Promise((res, rej) => {
    const srv = createServer();
    srv.on("error", rej);
    srv.listen(0, "127.0.0.1", () => {
      const port = (srv.address() as { port: number }).port;
      srv.close(() => res(port));
    });
  });
}

let cachedHash: string | null = null;
/** The password hash, made by the app's own `hash_password` (once per worker: it is a slow KDF). */
function passwordHash(): string {
  cachedHash ??= execFileSync(
    "uv",
    ["run", "python", "-c", "import sys; from agent_sessions.auth import hash_password; print(hash_password(sys.argv[1]))", PASSWORD],
    { cwd: REPO_ROOT, encoding: "utf8", env: cleanEnv() },
  ).trim();
  return cachedHash;
}

function cleanEnv(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(process.env).filter(([k, v]) => !k.startsWith("AGENT_SESSIONS_") && v !== undefined),
  ) as Record<string, string>;
}

export async function startApp(opts: { forcePasswordChange?: boolean } = {}): Promise<App> {
  const procs: ChildProcess[] = [];
  return withTempHome(
    "bl-served-",
    async (home) => {
      writeFileSync(join(home, "env"), `AGENT_SESSIONS_USERNAME=${USER}\n`);
      const port = await freePort();
      const base = `http://127.0.0.1:${port}`;
      let out = "";
      const app = spawn("uv", ["run", "agent-sessions", "serve", "--host", "127.0.0.1", "--port", String(port)], {
        cwd: REPO_ROOT,
        detached: true, // its own process group: `uv run` execs a child the group kill must reach
        env: {
          ...cleanEnv(),
          HOME: home,
          AGENT_SESSIONS_HOME: home,
          AGENT_SESSIONS_FS_ROOT: home,
          AGENT_SESSIONS_RUNTIME_DIR: join(home, "rt"),
          AGENT_SESSIONS_USERNAME: USER,
          AGENT_SESSIONS_PASSWORD_HASH: passwordHash(),
          AGENT_SESSIONS_SECRET_KEY: "served-pages-secret-".padEnd(64, "0"),
          AGENT_SESSIONS_ORIGIN: base,
          AGENT_SESSIONS_2FA_FILE: join(home, "2fa.json"),
          AGENT_SESSIONS_ENV_FILE: join(home, "env"),
          AGENT_SESSIONS_METADATA: join(home, "metadata.json"),
          AGENT_SESSIONS_PROJECTS: join(home, "projects.json"),
          AGENT_SESSIONS_FORCE_PASSWORD_CHANGE: opts.forcePasswordChange ? "1" : "0",
          PYTHONUNBUFFERED: "1",
        },
      });
      procs.push(app);
      app.on("error", (e) => (out += `spawn error: ${String(e)}\n`));
      app.stdout?.on("data", (d: Buffer) => (out += d.toString()));
      app.stderr?.on("data", (d: Buffer) => (out += d.toString()));
      const deadline = Date.now() + STARTUP_BUDGET_MS;
      for (;;) {
        try {
          if ((await fetch(`${base}/healthz`)).status < 500) break;
        } catch {
          /* not up yet */
        }
        if (app.exitCode !== null) throw new Error(`app exited (${app.exitCode}):\n${out}`);
        if (Date.now() > deadline) throw new Error(`app never answered /healthz:\n${out}`);
        await new Promise((r) => setTimeout(r, 250));
      }
      return {
        base,
        home,
        logs: () => out,
        stop: async () => {
          await killGroupsAndWait(procs);
          removeTree(home);
        },
      };
    },
    async () => {
      await killGroupsAndWait(procs);
    },
  );
}

/** RFC 6238 TOTP (SHA-1, 30 s, 6 digits) for a base32 secret — what an authenticator app shows. */
export function totp(secret: string, at = Date.now()): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of secret.replace(/=+$/, "").toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const key = Buffer.from(bits.match(/.{8}/g)!.map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 1000 / 30)));
  const h = createHmac("sha1", key).update(counter).digest();
  const o = h[h.length - 1] & 0xf;
  const n = ((h[o] & 0x7f) << 24) | (h[o + 1] << 16) | (h[o + 2] << 8) | h[o + 3];
  return String(n % 1_000_000).padStart(6, "0");
}
