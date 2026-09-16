import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { expect, type Page } from "@playwright/test";

import {
  hermeticGitEnv,
  killGroupsAndWait,
  removeTree,
  withTempHome,
  STARTUP_BUDGET_MS,
} from "./harness";

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
  stop: () => Promise<void>;
}

function git(cwd: string, ...args: string[]): void {
  // `hermeticGitEnv` strips EVERY `GIT_*`, not just the two config ones: `GIT_DIR` or
  // `GIT_WORK_TREE` in the caller's shell would send this fixture's `git init` and commit at a
  // repository that is not the throwaway one.
  execFileSync("git", args, { cwd, stdio: "pipe", env: hermeticGitEnv() });
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
    // Node emits `error` — NOT `exit` — for ENOENT / EAGAIN / EMFILE. With no listener that is an
    // UNCAUGHT exception, which takes the whole runner down before `withTempHome`'s catch can
    // kill the groups already started or remove the temp home. Rejecting turns it into an
    // ordinary failure that the cleanup fence handles like any other.
    child.on("error", (e) => {
      clearTimeout(timer);
      rej(new Error(`${what} failed to start: ${String(e)}\nOutput:\n${buf}`));
    });
  });
}

/** Attach an `error` listener the instant a child exists.
 *
 *  A ChildProcess `error` with no listener is an UNCAUGHT exception — Node's rule for
 *  EventEmitter — so ENOENT/EAGAIN/EMFILE would take the whole runner down before the cleanup
 *  fence could kill the groups already started or remove the temp home. The waiters reject on it
 *  too; this is the belt, covering the window before any waiter has subscribed.
 */
function guardSpawn(child: ChildProcess, key: string): void {
  child.on("error", (e) => {
    process.stderr.write(`[${key}] spawn error: ${String(e)}\n`);
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
  let spawnError: string | null = null;
  child.on("exit", (code) => {
    exited = code ?? -1;
  });
  // See `waitForLine`: an unhandled `error` is an uncaught exception, not a rejected promise, and
  // it kills the runner before the cleanup fence gets to run.
  child.on("error", (e) => {
    spawnError = String(e);
  });
  for (;;) {
    try {
      const r = await fetch(url);
      if (r.status < 500) return;
    } catch {
      /* not up yet */
    }
    if (spawnError !== null) {
      throw new Error(`${key} failed to start: ${spawnError}. Output:\n${logs[key]}`);
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
  // Declared before the fence so `killAll` can reach them from the failure path.
  const procs: ChildProcess[] = [];
  /** Kill every group started so far and WAIT for them to be gone. Safe to call twice, and
   *  never by name — a `pgrep -f "agent-sessions serve"` would also match the operator's OWN
   *  running service. Awaiting is what makes the subsequent delete safe rather than a race. */
  const killAll = () => killGroupsAndWait(procs);
  /** The app's runtime dir (dtach sockets + the #1006 hooks-void), declared before the fence for
   *  exactly the reason `procs` is: the failure path has to be able to remove it. */
  let runtimeDir = "";

  // EVERYTHING below runs inside the fence, seeding included. The previous shape created the
  // temp home, built a git repository in it, and only then opened a try/catch around the process
  // spawns — so a setup command that failed leaked `/tmp/bl-appmode-*` on every run. Handing the
  // directory in through a callback makes "nothing fallible happens outside the fence" a property
  // of the structure rather than a rule to remember.
  return withTempHome(
    "bl-appmode-",
    async (home) => {
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
  // The runtime dir must NOT sit under the temp HOME, and that is a production rule rather than a
  // test detail (#1006). `hooks_void()` hands git `core.hooksPath`, so it verifies that directory's
  // WHOLE ancestry is private — and the temp home is under `/tmp`, whose 1777 mode means any local
  // account can create a name we would then trust. The app refuses that by design, with no env-var
  // escape hatch, so the harness supplies a genuinely private directory instead of asking the app
  // to relax. `~/.cache` is 0700 under a home we own, which is the same shape production uses
  // (the installer sets `AGENT_SESSIONS_RUNTIME_DIR=$HOME/pty`), so this makes the stack MORE
  // faithful, not less. Without it the panel's branch switch is refused and never reaches disk —
  // measured: `appmode-git.spec.ts` failed with the repository still on `master`.
  mkdirSync(join(homedir(), ".cache"), { recursive: true });
  runtimeDir = mkdtempSync(join(homedir(), ".cache", "bl-e2e-rt-"));

  const env = {
    ...clean,
    HOME: home,
    AGENT_SESSIONS_HOME: home,
    AGENT_SESSIONS_FS_ROOT: home,
    AGENT_SESSIONS_RUNTIME_DIR: runtimeDir,
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

  return await boot();

  async function boot(): Promise<Stack> {
  // ONE budget for the whole boot, not three independent ones. Each wait below gets what is
  // LEFT of it, so three slow steps cannot add up past the hook that is waiting on us.
  const bootStarted = Date.now();
  const left = () => Math.max(1_000, STARTUP_BUDGET_MS - (Date.now() - bootStarted));
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
  guardSpawn(relay, "relay");
  capture(relay, "relay");
  const relayPort = Number(await waitForLine(relay, /RELAY_PORT=(\d+)/, "test relay", left()));

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
  guardSpawn(app, "app");
  capture(app, "app");
  await waitForHttp(`http://127.0.0.1:${appPort}/healthz`, app, logs, "app", left());

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
  guardSpawn(agent, "agent");
  capture(agent, "agent");
  await waitForLine(agent, /registered console/, "home-free agent registration", left());

  return {
    home,
    repo,
    relayPort,
    appPort,
    logs,
    stop: async () => {
      // AWAIT the group kill before deleting. `uv run` is a launcher, so the signal goes to the
      // group; and `process.kill()` returns before the processes are actually gone, so deleting
      // straight after it raced its own children — measured, a PASSING run left a temp home
      // behind containing the agent's `.claude.json`, written after the delete had walked past.
      await killAll();
      // `removeTree` stays as the belt: bounded, best-effort, never throws.
      removeTree(home);
      removeTree(runtimeDir);
    },
  };
  }
    },
    // Runs before the temp home is removed: a child still holding it as its cwd has to go first.
    // The runtime dir goes here too — `withTempHome` only knows about the home, so a boot that
    // threw would otherwise leak a `~/.cache/bl-e2e-rt-*` on every failed run, which is the same
    // accumulation `removeTree`'s own header records having been bitten by.
    async () => {
      await killAll();
      if (runtimeDir) removeTree(runtimeDir);
    },
  );
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
