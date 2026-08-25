import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { expect, type Page } from "@playwright/test";

/** A real app-mode stack for the Home Free E2E (#579, #806, #807).
 *
 *  Every piece here is the production one except the relay, and the relay is the one piece where a
 *  stand-in loses nothing: it is **blind** by design — it authenticates the agent by signature,
 *  pairs a viewer with it, and pipes ciphertext. `tests/appmode/relay.py` does exactly that,
 *  including the Ed25519 registration check and the ALTCHA challenge the connect page will not
 *  proceed without.
 *
 *  So what these tests actually exercise is the whole real path: the browser's connect page, the
 *  real Noise-style handshake, the real mux with its real flow control, `tunnel.fetch`, the real
 *  agent process, `AppProxyTarget`'s Origin/CSRF rewriting, the real FastAPI app, and a real git
 *  repository on disk. That is the boundary #806/#807 name as acceptance evidence, and it is not
 *  reachable from a test that mocks `/api/**` at the network edge.
 */

const REPO_ROOT = resolve(process.cwd(), "..");

/** Is the Python half of the stack reachable from here?
 *
 *  `web-ci` is deliberately Node-only — its own header says it never touches the gated Python
 *  workflow — so `uv` is absent there and these specs must SKIP rather than fail. They get their
 *  own workflow (`appmode-e2e.yml`) which provisions both halves, and they run locally wherever
 *  the toolchain exists. A spec that cannot run says so; it does not pretend to pass.
 */
export const PY_STACK_AVAILABLE = (() => {
  try {
    execFileSync("uv", ["--version"], { stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
})();
const ACCESS_KEY = "test-access-key-0123456789abcdef";
const CONSOLE_NAME = "e2e-box";
export const SESSION_UUID = "aaaaaaaa-0000-4000-8000-0000000000e2";

export interface Stack {
  home: string;
  /** The seeded session's cwd — a real git repository. */
  repo: string;
  relayPort: number;
  appPort: number;
  /** Everything the three children printed. A failure in CI is otherwise undiagnosable: the
   *  interesting error is usually in the agent's log, not in the browser. */
  logs: Record<string, string>;
  stop: () => void;
}

function git(cwd: string, ...args: string[]): void {
  execFileSync("git", args, {
    cwd,
    stdio: "pipe",
    env: { ...process.env, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_SYSTEM: "/dev/null" },
  });
}

/** Wait for a line matching `re` on a child's stdout/stderr, or reject with what it did print. */
function waitForLine(child: ChildProcess, re: RegExp, what: string, ms = 45_000): Promise<string> {
  return new Promise((res, rej) => {
    let buf = "";
    const timer = setTimeout(() => rej(new Error(`${what} never appeared. Output:\n${buf}`)), ms);
    const onData = (d: Buffer) => {
      buf += d.toString();
      const m = buf.match(re);
      if (m) {
        clearTimeout(timer);
        res(m[1] ?? m[0]);
      }
    };
    child.stdout?.on("data", onData);
    child.stderr?.on("data", onData);
    child.on("exit", (code) => {
      clearTimeout(timer);
      rej(new Error(`${what} exited early (${code}). Output:\n${buf}`));
    });
  });
}

/** Poll `url` until it answers — and if it never does, say WHY.
 *
 *  The first version threw a bare "timed out waiting for …", which is worth almost nothing in CI:
 *  it cannot distinguish "still starting under load" from "died on an unset env var". The child's
 *  captured output is the whole diagnosis, so it goes in the error, and an exited child fails
 *  immediately rather than burning the full budget first.
 */
async function waitForHttp(
  url: string,
  child: ChildProcess,
  logs: Record<string, string>,
  key: string,
  ms = 120_000,
): Promise<void> {
  const deadline = Date.now() + ms;
  let exited: number | null = null;
  child.on("exit", (code) => {
    exited = code ?? -1;
  });
  for (;;) {
    try {
      const r = await fetch(url);
      if (r.status < 500) return;
    } catch {
      /* not up yet */
    }
    if (exited !== null) {
      throw new Error(`${key} exited (${exited}) before serving ${url}. Output:\n${logs[key]}`);
    }
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${url}. ${key} output:\n${logs[key]}`);
    }
    await new Promise((r) => setTimeout(r, 250));
  }
}

/** A port the OS says is free, rather than one derived from the pid.
 *
 *  `41900 + pid % 300` collides: the shared runner hosts several jobs at once, and two of them
 *  picking the same number is a failure that looks exactly like "the app did not start". */
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

/** Boot app + relay + agent against a throwaway HOME holding one seeded session and one repo. */
export async function startStack(): Promise<Stack> {
  const home = mkdtempSync(join(tmpdir(), "bl-appmode-"));
  const repo = join(home, "proj");

  // A real repository — the branch switch under test moves a real ref on real disk.
  mkdirSync(repo, { recursive: true });
  git(repo, "init", "-q");
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  writeFileSync(join(repo, "a.txt"), "one\n");
  git(repo, "add", "a.txt");
  git(repo, "commit", "-qm", "init");
  git(repo, "branch", "other");

  // One Claude-shaped session whose cwd is that repository, so the panel has somewhere to dock.
  const projSlug = repo.replace(/\//g, "-");
  const projDir = join(home, ".claude", "projects", projSlug);
  mkdirSync(projDir, { recursive: true });
  writeFileSync(
    join(projDir, `${SESSION_UUID}.jsonl`),
    `${JSON.stringify({ type: "user", cwd: repo, message: { content: "app-mode e2e" } })}\n`,
  );

  const procs: ChildProcess[] = [];
  const logs: Record<string, string> = { relay: "", app: "", agent: "" };

  // Everything `AGENT_SESSIONS_*` is STRIPPED from the inherited environment and set explicitly
  // below. Inheriting it is what made this harness pass here and fail in CI three times running:
  // this host exports `AGENT_SESSIONS_SECRET_KEY` into every shell, so the app started locally on
  // a value CI does not have and died there with `missing required env var`. A test stack that
  // reads anything from the developer's shell is not a test stack — it is a coincidence.
  const clean = Object.fromEntries(
    Object.entries(process.env).filter(([k]) => !k.startsWith("AGENT_SESSIONS_")),
  ) as Record<string, string>;
  const capture = (child: ChildProcess, key: string) => {
    const add = (d: Buffer) => {
      logs[key] += d.toString();
    };
    child.stdout?.on("data", add);
    child.stderr?.on("data", add);
  };
  const env = {
    ...clean,
    HOME: home,
    AGENT_SESSIONS_HOME: home,
    AGENT_SESSIONS_FS_ROOT: home,
    AGENT_SESSIONS_AUTH_MODE: "none",
    // `create_app` reads these regardless of auth mode, so they are supplied rather than
    // inherited. Throwaway values for a throwaway stack.
    AGENT_SESSIONS_USERNAME: "e2e",
    AGENT_SESSIONS_PASSWORD_HASH: "x",
    AGENT_SESSIONS_SECRET_KEY: "e2e-secret-key-".padEnd(64, "0"),
    AGENT_SESSIONS_2FA_FILE: join(home, "2fa.json"),
    AGENT_SESSIONS_METADATA: join(home, "metadata.json"),
    AGENT_SESSIONS_PROJECTS: join(home, "projects.json"),
  };

  /** Kill every group started so far and remove the temp home. Safe to call twice. */
  const teardown = () => {
    for (const c of procs) {
      try {
        if (c.pid) process.kill(-c.pid, "SIGKILL");
      } catch {
        /* group already gone */
      }
      try {
        c.kill("SIGKILL");
      } catch {
        /* already gone */
      }
    }
    rmSync(home, { recursive: true, force: true });
  };

  try {
    return await boot();
  } catch (e) {
    // Any failure AFTER the first spawn used to leak its detached group and the temp home — and
    // the very first CI boot failure did exactly that. A partial start now cleans up after
    // itself, so a red regression cannot slowly exhaust a shared runner.
    teardown();
    throw e;
  }

  async function boot(): Promise<Stack> {
  // 1. The relay (blind), on an ephemeral port it reports back.
  const relay = spawn("uv", ["run", "python", "tests/appmode/relay.py", "--port", "0"], {
    cwd: REPO_ROOT,
    env,
    // Its own process GROUP. `uv run` execs a child python, so killing the `uv` pid leaves the
    // real process behind — leaking a relay, an app and an agent per run until the host runs out
    // of threads. (It did: `Resource temporarily unavailable` from the bundler, at load 36.)
    detached: true,
  });
  procs.push(relay);
  capture(relay, "relay");
  const relayPort = Number(await waitForLine(relay, /RELAY_PORT=(\d+)/, "test relay", 120_000));

  // 2. The real app.
  const appPort = await freePort();
  const app = spawn(
    "uv",
    ["run", "agent-sessions", "serve", "--host", "127.0.0.1", "--port", String(appPort)],
    {
      cwd: REPO_ROOT,
      env: { ...env, AGENT_SESSIONS_ORIGIN: `http://127.0.0.1:${appPort}` },
      detached: true,
    },
  );
  procs.push(app);
  capture(app, "app");
  await waitForHttp(`http://127.0.0.1:${appPort}/healthz`, app, logs, "app");

  // 3. The real agent, dialling the relay and bridging to the app.
  const agent = spawn("uv", ["run", "python", "-m", "agent_sessions.homefree"], {
    cwd: REPO_ROOT,
    detached: true,
    env: {
      ...env,
      HOMEFREE_RELAY_URL: `ws://127.0.0.1:${relayPort}/relay/ws`,
      HOMEFREE_CONSOLE_NAME: CONSOLE_NAME,
      HOMEFREE_ACCESS_KEY: ACCESS_KEY,
      HOMEFREE_IDENTITY_PATH: join(home, "identity.key"),
      HOMEFREE_APP_PORT: String(appPort),
      HOMEFREE_APP_ORIGIN: `http://127.0.0.1:${appPort}`,
      PYTHONUNBUFFERED: "1",
    },
  });
  procs.push(agent);
  capture(agent, "agent");
  await waitForLine(agent, /registered console/, "home-free agent registration", 120_000);

  return {
    home,
    repo,
    relayPort,
    appPort,
    logs,
    stop: () => {
      for (const p of procs) {
        // Kill the GROUP (negative pid), not the direct child. `uv run` is a launcher: signalling
        // it leaves the python it exec'd running forever. Never match these back by name — a
        // `pgrep -f "agent-sessions serve"` also matches the operator's OWN running service, and
        // killing that takes their live BattleLab down.
        try {
          if (p.pid) process.kill(-p.pid, "SIGKILL");
        } catch {
          /* group already gone */
        }
        try {
          p.kill("SIGKILL");
        } catch {
          /* already gone */
        }
      }
      rmSync(home, { recursive: true, force: true });
    },
  };
  }
}

/** Drive the real connect page all the way to a mounted SPA over the tunnel. */
export async function connect(page: Page, stack: Stack): Promise<void> {
  // The human gate's hold is shortened through the page's own harness seam — the same one the
  // existing connect specs use. Nothing else is stubbed: the socket, handshake and mux are real.
  await page.addInitScript(() => {
    window.__battlelabConnectHarness = { holdMs: 50 };
  });
  await page.goto("/connect.html");
  // The relay field lives behind "Custom relay" — the public deploy defaults it, a local one
  // does not, so the test supplies it the same way an operator running their own relay would.
  await page.locator("#relay-options summary").click();
  await page.fill("#relay", `http://127.0.0.1:${stack.relayPort}`);
  await page.fill("#name", CONSOLE_NAME);
  await page.fill("#key", ACCESS_KEY);

  const hold = page.locator("#gate-hold");
  await hold.hover();
  await page.mouse.down();
  await expect(page.locator("#verify-gate")).toHaveAttribute("data-state", "verified", {
    timeout: 10_000,
  });
  await page.mouse.up();

  await page.locator("#connect").click();
  // The streamed SPA takes the screen once the tunnel is up and the app bundle has mounted into
  // the connect page's own `#app-root`. `body[data-state="connected"]` is what reveals it.
  await expect(page.locator("body")).toHaveAttribute("data-state", "connected", {
    timeout: 60_000,
  });
  await expect(page.locator("#app-root")).toBeVisible({ timeout: 60_000 });
}
