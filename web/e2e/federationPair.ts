/** Two real BattleLab apps for the pairing E2E (#1246 P2, #2052).
 *
 *  Each is `tests/appmode/federation_instance.py`: the production `create_app` under uvicorn with
 *  its own HOME and its own federation directory, so each has its own identity key, peer store and
 *  owner lock. They reach each other over loopback through the real `/ws/fed` door and the real
 *  dialer. The lifecycle rules are appmode.ts's, for the same reasons: every child in its own
 *  process group, killed and AWAITED before its directories are removed, and nothing fallible
 *  outside the fence. */
import { spawn, type ChildProcess } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

import { freePort, guardSpawn, waitForHttp } from "./appmode";
import { killGroupsAndWait, removeTree, withTempHome, STARTUP_BUDGET_MS } from "./harness";

const REPO_ROOT = resolve(process.cwd(), "..");

export interface FederationInstance {
  name: string;
  /** `http://127.0.0.1:<port>`: the origin the browser opens and the app checks writes against. */
  origin: string;
  /** An empty folder under this instance's own HOME: where its sessions work. */
  workdir: string;
}

export interface FederationPair {
  a: FederationInstance;
  b: FederationInstance;
  logs: Record<string, string>;
  stop: () => Promise<void>;
}

/** `terminal`: a shell session id. alpha then hosts a REAL terminal session under it (a `dtach`
 *  master running `cat`, see `federation_instance.py`), for the spec that shares one (#2060).
 *
 *  `api`: BOTH instances can run real API sessions (#2055, #2061, #2062): the in-tree `codex-api`
 *  client on a scripted Codex (`tests/fake_native.py`), each generation the real worker process.
 *  A spec creates one through the app's own route and drives it by typing in the page. */
export async function startFederationPair(
  opts: { terminal?: string; api?: boolean } = {},
): Promise<FederationPair> {
  const procs: ChildProcess[] = [];
  const runtimeDirs: string[] = [];
  const killAll = () => killGroupsAndWait(procs);
  return withTempHome(
    "bl-fedpair-",
    async (root) => {
      const started = Date.now();
      const left = () => Math.max(1_000, STARTUP_BUDGET_MS - (Date.now() - started));
      const logs: Record<string, string> = {};
      // Nothing `AGENT_SESSIONS_*` is inherited from the developer's shell (appmode.ts).
      const clean = Object.fromEntries(
        Object.entries(process.env).filter(([k]) => !k.startsWith("AGENT_SESSIONS_")),
      ) as Record<string, string>;
      mkdirSync(join(homedir(), ".cache"), { recursive: true });

      const start = async (name: string, extra: string[] = []): Promise<FederationInstance> => {
        const home = join(root, name);
        const prefs = join(home, ".config", "agent-sessions");
        mkdirSync(prefs, { recursive: true });
        // A returning operator: no onboarding, no What's new dialog over the page under test.
        writeFileSync(
          join(prefs, "prefs.json"),
          JSON.stringify({ onboarded: true, whats_new_seen: "999.0.0" }),
        );
        // Not under /tmp: the app verifies its runtime dir's whole ancestry is private (#1006).
        const runtimeDir = mkdtempSync(join(homedir(), ".cache", "bl-e2e-rt-"));
        runtimeDirs.push(runtimeDir);
        const workdir = join(home, "work");
        mkdirSync(workdir, { recursive: true });
        const args = [...extra];
        if (opts.api) {
          // The workers' private sockets. Short: an AF_UNIX path is at most 107 bytes, and the
          // runner's TMPDIR is not. Removed with the rest.
          const sockets = mkdtempSync("/tmp/blfed-");
          runtimeDirs.push(sockets);
          args.push("--api", sockets);
        }
        const port = await freePort();
        const origin = `http://127.0.0.1:${port}`;
        const child = spawn(
          "uv",
          [
            "run",
            "python",
            "tests/appmode/federation_instance.py",
            "--port",
            String(port),
            "--name",
            name,
            ...args,
          ],
          {
            cwd: REPO_ROOT,
            // Its own process GROUP: `uv run` execs a child python.
            detached: true,
            env: {
              ...clean,
              HOME: home,
              AGENT_SESSIONS_HOME: home,
              AGENT_SESSIONS_FS_ROOT: home,
              AGENT_SESSIONS_RUNTIME_DIR: runtimeDir,
              AGENT_SESSIONS_FEDERATION_DIR: join(home, "federation"),
              AGENT_SESSIONS_AUTH_MODE: "none",
              AGENT_SESSIONS_ORIGIN: origin,
              AGENT_SESSIONS_USERNAME: "e2e",
              AGENT_SESSIONS_PASSWORD_HASH: "x",
              AGENT_SESSIONS_SECRET_KEY: `e2e-secret-key-${name}-`.padEnd(64, "0"),
              AGENT_SESSIONS_2FA_FILE: join(home, "2fa.json"),
              AGENT_SESSIONS_METADATA: join(home, "metadata.json"),
              AGENT_SESSIONS_PROJECTS: join(home, "projects.json"),
              // The operator's project folder: where a new session of theirs starts by default.
              // Only for the API specs: a root is also a scope boundary, and the terminal spec's
              // session works in HOME, outside it.
              ...(opts.api ? { AGENT_SESSIONS_PROJECT_ROOTS: workdir } : {}),
              PYTHONUNBUFFERED: "1",
            },
          },
        );
        procs.push(child);
        guardSpawn(child, name);
        logs[name] = "";
        const add = (d: Buffer) => {
          logs[name] += d.toString();
        };
        child.stdout?.on("data", add);
        child.stderr?.on("data", add);
        await waitForHttp(`${origin}/healthz`, child, logs, name, left());
        return { name, origin, workdir };
      };

      const a = await start("alpha", opts.terminal ? ["--terminal", opts.terminal] : []);
      const b = await start("beta");
      return {
        a,
        b,
        logs,
        stop: async () => {
          await killAll();
          removeTree(root);
          for (const dir of runtimeDirs) removeTree(dir);
        },
      };
    },
    async () => {
      await killAll();
      for (const dir of runtimeDirs) removeTree(dir);
    },
  );
}
